package io.oxidebatch.workloads.postgres.benchmark.rawjdbc;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Locale;
import java.util.Objects;
import java.util.Properties;
import java.util.Set;

/**
 * Raw Java/JDBC semantic-parity control for campaign #79.
 *
 * <p>This candidate deliberately owns only the minimal durability needed for attribution. It has no
 * Spring or OxideBatch dependency and writes framework metadata nowhere. Source identity is computed
 * while holding a PostgreSQL SHARE table lock which remains held through the protected read. Business
 * writes and benchmark-owned checkpoint advancement commit atomically on one destination connection.
 */
public final class RawJdbcMain {
    private static final String CURSOR_DEFINITION_REVISION = "raw-jdbc-postgres-postgres-cursor-v1";
    private static final String PAGING_DEFINITION_REVISION = "raw-jdbc-postgres-postgres-paging-v1";
    private static final int DEFAULT_FETCH_SIZE = 500;
    private static final int DEFAULT_PAGE_SIZE = 750;
    private static final int DEFAULT_CHUNK_SIZE = 1_000;
    private static final int COLUMNS_PER_ROW = 7;
    private static final int MAX_PARAMETERS_PER_STATEMENT = 2_000;
    private static final int ROWS_PER_STATEMENT = MAX_PARAMETERS_PER_STATEMENT / COLUMNS_PER_ROW;
    private static final int MAX_BOUND_PARAMETERS = ROWS_PER_STATEMENT * COLUMNS_PER_ROW;
    private static final int MAX_CHUNK_SIZE = 1_000_000;
    private static final int MAX_READ_BATCH_SIZE = 1_000_000;
    private static final int FINGERPRINT_LEN = 16;
    private static final long PREMIUM_THRESHOLD_CENTS = 50_000L;

    private RawJdbcMain() {}

    public static void main(String[] args) throws Exception {
        Cli cli = Cli.parse(args);
        switch (cli.command()) {
            case "migrate" -> {
                cli.options().requireOnly();
                migrate(cli.database());
            }
            case "run" -> run(cli.database(), RunConfig.parse(cli.options()));
            case "inspect" -> {
                cli.options().requireOnly("import-name");
                inspect(cli.database(), required(cli.options(), "import-name"));
            }
            default -> throw new IllegalArgumentException("unknown command: " + cli.command());
        }
    }

    private static void migrate(DatabaseConfig database) throws SQLException {
        try (Connection connection = connect(database); Statement statement = connection.createStatement()) {
            statement.execute("CREATE SCHEMA IF NOT EXISTS benchmark_java");
            statement.execute(
                    "CREATE TABLE IF NOT EXISTS benchmark_java.raw_checkpoint ("
                            + "import_name TEXT NOT NULL, "
                            + "source_digest TEXT NOT NULL, "
                            + "reader_mode TEXT NOT NULL, "
                            + "definition_revision TEXT NOT NULL, "
                            + "last_customer_id BIGINT NOT NULL, "
                            + "committed_chunks BIGINT NOT NULL, "
                            + "committed_rows BIGINT NOT NULL, "
                            + "PRIMARY KEY (import_name, source_digest, reader_mode, definition_revision))");
        }
    }

    private static void run(DatabaseConfig database, RunConfig config) throws Exception {
        try (Connection source = connect(database)) {
            source.setAutoCommit(false);
            try (Statement lock = source.createStatement()) {
                lock.execute("LOCK TABLE app_source.source_customer IN SHARE MODE");
            }

            String sourceDigest = sourceDigest(source);
            ExecutionIdentity identity = new ExecutionIdentity(
                    config.importName(), sourceDigest, config.readerMode(), config.definitionRevision());

            try (Connection destination = connect(database)) {
                destination.setAutoCommit(false);
                rejectConflictingSourceIdentity(destination, identity);
                Checkpoint checkpoint = loadCheckpoint(destination, identity);
                RunState state = new RunState(checkpoint, config.chunkSize());

                if (config.readerMode() == ReaderMode.CURSOR) {
                    runCursor(source, destination, identity, config, state);
                } else {
                    runPaging(source, destination, identity, config, state);
                }
                flushChunk(destination, identity, config, state);

                System.out.printf(
                        Locale.ROOT,
                        "source_digest=%s reader_mode=%s last_customer_id=%s committed_chunks=%d committed_rows=%d%n",
                        sourceDigest,
                        config.readerMode().value,
                        state.lastCustomerId == null ? "none" : state.lastCustomerId,
                        state.committedChunks,
                        state.committedRows);
            } finally {
                source.rollback();
            }
        }
    }

    private static void runCursor(
            Connection source,
            Connection destination,
            ExecutionIdentity identity,
            RunConfig config,
            RunState state)
            throws Exception {
        String sql;
        if (state.lastCustomerId == null) {
            sql = "SELECT customer_id, full_name, is_active, balance_cents "
                    + "FROM app_source.source_customer ORDER BY customer_id";
        } else {
            sql = "SELECT customer_id, full_name, is_active, balance_cents "
                    + "FROM app_source.source_customer WHERE customer_id > ? ORDER BY customer_id";
        }
        try (PreparedStatement statement = source.prepareStatement(
                        sql, ResultSet.TYPE_FORWARD_ONLY, ResultSet.CONCUR_READ_ONLY)) {
            statement.setFetchSize(config.readBatchSize());
            if (state.lastCustomerId != null) {
                statement.setLong(1, state.lastCustomerId);
            }
            try (ResultSet rows = statement.executeQuery()) {
                while (rows.next()) {
                    acceptSourceRow(destination, identity, config, state, sourceRow(rows));
                }
            }
        }
    }

    private static void runPaging(
            Connection source,
            Connection destination,
            ExecutionIdentity identity,
            RunConfig config,
            RunState state)
            throws Exception {
        Long readPosition = state.lastCustomerId;
        while (true) {
            List<SourceRow> page = fetchPage(source, readPosition, config.readBatchSize());
            if (page.isEmpty()) {
                return;
            }
            readPosition = page.get(page.size() - 1).customerId();
            for (SourceRow row : page) {
                acceptSourceRow(destination, identity, config, state, row);
            }
        }
    }

    private static List<SourceRow> fetchPage(Connection source, Long afterCustomerId, int pageSize)
            throws SQLException {
        String sql;
        if (afterCustomerId == null) {
            sql = "SELECT customer_id, full_name, is_active, balance_cents "
                    + "FROM app_source.source_customer ORDER BY customer_id LIMIT ?";
        } else {
            sql = "SELECT customer_id, full_name, is_active, balance_cents "
                    + "FROM app_source.source_customer WHERE customer_id > ? ORDER BY customer_id LIMIT ?";
        }
        List<SourceRow> page = new ArrayList<>(pageSize);
        try (PreparedStatement statement = source.prepareStatement(
                        sql, ResultSet.TYPE_FORWARD_ONLY, ResultSet.CONCUR_READ_ONLY)) {
            int parameter = 1;
            if (afterCustomerId != null) {
                statement.setLong(parameter++, afterCustomerId);
            }
            statement.setInt(parameter, pageSize);
            try (ResultSet rows = statement.executeQuery()) {
                while (rows.next()) {
                    page.add(sourceRow(rows));
                }
            }
        }
        return page;
    }

    private static SourceRow sourceRow(ResultSet row) throws SQLException {
        return new SourceRow(
                row.getLong("customer_id"),
                row.getString("full_name"),
                row.getBoolean("is_active"),
                row.getLong("balance_cents"));
    }

    private static String sourceDigest(Connection source) throws SQLException, NoSuchAlgorithmException {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        try (PreparedStatement statement = source.prepareStatement(
                        "SELECT customer_id, full_name, is_active, balance_cents "
                                + "FROM app_source.source_customer ORDER BY customer_id",
                        ResultSet.TYPE_FORWARD_ONLY,
                        ResultSet.CONCUR_READ_ONLY)) {
            statement.setFetchSize(DEFAULT_FETCH_SIZE);
            try (ResultSet rows = statement.executeQuery()) {
                while (rows.next()) {
                    digest.update(littleEndian(rows.getLong("customer_id")));
                    digest.update((byte) 0);
                    digest.update(rows.getString("full_name").getBytes(StandardCharsets.UTF_8));
                    digest.update((byte) 0);
                    digest.update((byte) (rows.getBoolean("is_active") ? 1 : 0));
                    digest.update(littleEndian(rows.getLong("balance_cents")));
                    digest.update((byte) 0xff);
                }
            }
        }
        return HexFormat.of().formatHex(digest.digest());
    }

    private static byte[] littleEndian(long value) {
        return ByteBuffer.allocate(Long.BYTES).order(ByteOrder.LITTLE_ENDIAN).putLong(value).array();
    }

    private static void rejectConflictingSourceIdentity(Connection destination, ExecutionIdentity identity)
            throws SQLException {
        String sql = "SELECT source_digest FROM benchmark_java.raw_checkpoint "
                + "WHERE import_name = ? AND reader_mode = ? AND definition_revision = ? "
                + "AND source_digest <> ? ORDER BY source_digest LIMIT 1";
        try (PreparedStatement statement = destination.prepareStatement(sql)) {
            statement.setString(1, identity.importName());
            statement.setString(2, identity.readerMode().value);
            statement.setString(3, identity.definitionRevision());
            statement.setString(4, identity.sourceDigest());
            try (ResultSet row = statement.executeQuery()) {
                if (row.next()) {
                    throw new IllegalStateException(
                            "source digest changed for existing raw Java execution identity: import_name="
                                    + identity.importName()
                                    + " reader_mode="
                                    + identity.readerMode().value
                                    + " definition_revision="
                                    + identity.definitionRevision()
                                    + " existing_digest="
                                    + row.getString(1)
                                    + " current_digest="
                                    + identity.sourceDigest()
                                    + "; refusing stale checkpoint reuse");
                }
            }
        }
    }

    private static Checkpoint loadCheckpoint(Connection destination, ExecutionIdentity identity)
            throws SQLException {
        String sql = "SELECT last_customer_id, committed_chunks, committed_rows "
                + "FROM benchmark_java.raw_checkpoint "
                + "WHERE import_name = ? AND source_digest = ? AND reader_mode = ? AND definition_revision = ?";
        try (PreparedStatement statement = destination.prepareStatement(sql)) {
            bindIdentity(statement, identity, 1);
            try (ResultSet row = statement.executeQuery()) {
                if (row.next()) {
                    return new Checkpoint(row.getLong(1), row.getLong(2), row.getLong(3));
                }
            }
        }
        return new Checkpoint(null, 0L, 0L);
    }

    private static void acceptSourceRow(
            Connection destination,
            ExecutionIdentity identity,
            RunConfig config,
            RunState state,
            SourceRow row)
            throws Exception {
        state.chunk.add(project(identity, row));
        if (state.chunk.size() == config.chunkSize()) {
            flushChunk(destination, identity, config, state);
        }
    }

    private static ProjectedRow project(ExecutionIdentity identity, SourceRow source)
            throws NoSuchAlgorithmException {
        if (source.fullName().trim().isEmpty()) {
            throw new IllegalArgumentException("source full_name must not be empty for customer " + source.customerId());
        }
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        digest.update(littleEndian(source.customerId()));
        digest.update((byte) 0);
        digest.update(source.fullName().getBytes(StandardCharsets.UTF_8));
        digest.update((byte) 0);
        digest.update((byte) (source.active() ? 1 : 0));
        digest.update(littleEndian(source.balanceCents()));
        byte[] fullFingerprint = digest.digest();
        byte[] fingerprint = new byte[FINGERPRINT_LEN];
        System.arraycopy(fullFingerprint, 0, fingerprint, 0, FINGERPRINT_LEN);
        return new ProjectedRow(
                identity.importName(),
                identity.sourceDigest(),
                source.customerId(),
                source.fullName().toUpperCase(Locale.ROOT),
                source.balanceCents() / 100L,
                source.balanceCents() >= PREMIUM_THRESHOLD_CENTS,
                fingerprint);
    }

    private static void flushChunk(
            Connection destination,
            ExecutionIdentity identity,
            RunConfig config,
            RunState state)
            throws Exception {
        if (state.chunk.isEmpty()) {
            return;
        }
        long nextChunks = Math.addExact(state.committedChunks, 1L);
        long nextRows = Math.addExact(state.committedRows, state.chunk.size());
        long lastCustomerId = state.chunk.get(state.chunk.size() - 1).customerId();
        commitChunk(destination, identity, config, state.chunk, new Checkpoint(lastCustomerId, nextChunks, nextRows));
        state.lastCustomerId = lastCustomerId;
        state.committedChunks = nextChunks;
        state.committedRows = nextRows;
        state.chunk.clear();
    }

    private static void commitChunk(
            Connection destination,
            ExecutionIdentity identity,
            RunConfig config,
            List<ProjectedRow> chunk,
            Checkpoint checkpoint)
            throws Exception {
        try {
            for (int start = 0; start < chunk.size(); start += ROWS_PER_STATEMENT) {
                int end = Math.min(start + ROWS_PER_STATEMENT, chunk.size());
                insertBatch(destination, chunk.subList(start, end));
            }
            if (config.failAfterChunk() != null && config.failAfterChunk() == checkpoint.committedChunks()) {
                throw new InjectedFailure("injected failure after business writes before checkpoint/commit at chunk "
                        + checkpoint.committedChunks());
            }
            upsertCheckpoint(destination, identity, checkpoint);
            destination.commit();
        } catch (Exception failure) {
            try {
                destination.rollback();
            } catch (SQLException rollbackFailure) {
                failure.addSuppressed(rollbackFailure);
            }
            throw failure;
        }
    }

    private static void insertBatch(Connection destination, List<ProjectedRow> batch) throws SQLException {
        int boundParameters = Math.multiplyExact(batch.size(), COLUMNS_PER_ROW);
        if (batch.isEmpty() || batch.size() > ROWS_PER_STATEMENT || boundParameters > MAX_BOUND_PARAMETERS) {
            throw new IllegalArgumentException(
                    "writer batch exceeds parity bound of " + ROWS_PER_STATEMENT + " rows / "
                            + MAX_BOUND_PARAMETERS + " parameters");
        }
        StringBuilder sql = new StringBuilder(
                "INSERT INTO app_business.customer_projection "
                        + "(import_name, source_digest, customer_id, display_name, loyalty_score, is_premium, row_fingerprint) VALUES ");
        for (int row = 0; row < batch.size(); row++) {
            if (row != 0) {
                sql.append(',');
            }
            sql.append("(?,?,?,?,?,?,?)");
        }
        try (PreparedStatement statement = destination.prepareStatement(sql.toString())) {
            int parameter = 1;
            for (ProjectedRow row : batch) {
                statement.setString(parameter++, row.importName());
                statement.setString(parameter++, row.sourceDigest());
                statement.setLong(parameter++, row.customerId());
                statement.setString(parameter++, row.displayName());
                statement.setLong(parameter++, row.loyaltyScore());
                statement.setBoolean(parameter++, row.premium());
                statement.setBytes(parameter++, row.fingerprint());
            }
            int affected = statement.executeUpdate();
            if (affected != batch.size()) {
                throw new SQLException("writer affected " + affected + " rows for expected batch size " + batch.size());
            }
        }
    }

    private static void upsertCheckpoint(
            Connection destination, ExecutionIdentity identity, Checkpoint checkpoint) throws SQLException {
        String sql = "INSERT INTO benchmark_java.raw_checkpoint "
                + "(import_name, source_digest, reader_mode, definition_revision, last_customer_id, committed_chunks, committed_rows) "
                + "VALUES (?, ?, ?, ?, ?, ?, ?) "
                + "ON CONFLICT (import_name, source_digest, reader_mode, definition_revision) "
                + "DO UPDATE SET last_customer_id = EXCLUDED.last_customer_id, "
                + "committed_chunks = EXCLUDED.committed_chunks, committed_rows = EXCLUDED.committed_rows";
        try (PreparedStatement statement = destination.prepareStatement(sql)) {
            int parameter = bindIdentity(statement, identity, 1);
            statement.setLong(parameter++, Objects.requireNonNull(checkpoint.lastCustomerId()));
            statement.setLong(parameter++, checkpoint.committedChunks());
            statement.setLong(parameter, checkpoint.committedRows());
            statement.executeUpdate();
        }
    }

    private static int bindIdentity(PreparedStatement statement, ExecutionIdentity identity, int parameter)
            throws SQLException {
        statement.setString(parameter++, identity.importName());
        statement.setString(parameter++, identity.sourceDigest());
        statement.setString(parameter++, identity.readerMode().value);
        statement.setString(parameter++, identity.definitionRevision());
        return parameter;
    }

    private static void inspect(DatabaseConfig database, String importName) throws SQLException {
        try (Connection connection = connect(database);
                PreparedStatement statement = connection.prepareStatement(
                        "SELECT source_digest, reader_mode, definition_revision, last_customer_id, committed_chunks, committed_rows "
                                + "FROM benchmark_java.raw_checkpoint WHERE import_name = ? "
                                + "ORDER BY reader_mode, definition_revision, source_digest")) {
            statement.setString(1, importName);
            try (ResultSet rows = statement.executeQuery()) {
                while (rows.next()) {
                    System.out.printf(
                            Locale.ROOT,
                            "source_digest=%s reader_mode=%s definition_revision=%s last_customer_id=%d committed_chunks=%d committed_rows=%d%n",
                            rows.getString(1), rows.getString(2), rows.getString(3), rows.getLong(4), rows.getLong(5), rows.getLong(6));
                }
            }
        }
    }

    private static Connection connect(DatabaseConfig database) throws SQLException {
        if (database.url().toLowerCase(Locale.ROOT).contains("rewritebatchedinserts=true")) {
            throw new IllegalArgumentException("reWriteBatchedInserts=true is forbidden for the parity candidate");
        }
        Properties properties = new Properties();
        properties.setProperty("user", database.user());
        properties.setProperty("password", database.password());
        properties.setProperty("ApplicationName", "oxide-batch-workloads-raw-jdbc");
        properties.setProperty("reWriteBatchedInserts", "false");
        return DriverManager.getConnection(database.url(), properties);
    }

    private static String required(Options options, String key) {
        String value = options.values().get(key);
        if (value == null || value.isEmpty()) {
            throw new IllegalArgumentException("missing required option --" + key);
        }
        return value;
    }

    private static int positiveInt(Options options, String key, int defaultValue, int maximum) {
        String raw = options.values().get(key);
        int value = raw == null ? defaultValue : Integer.parseInt(raw);
        if (value < 1 || value > maximum) {
            throw new IllegalArgumentException("--" + key + " must be between 1 and " + maximum);
        }
        return value;
    }

    private static Long optionalPositiveLong(Options options, String key) {
        String raw = options.values().get(key);
        if (raw == null) {
            return null;
        }
        long value = Long.parseLong(raw);
        if (value < 1) {
            throw new IllegalArgumentException("--" + key + " must be greater than zero");
        }
        return value;
    }

    private enum ReaderMode {
        CURSOR("cursor"),
        PAGING("paging");

        private final String value;

        ReaderMode(String value) {
            this.value = value;
        }

        static ReaderMode parse(String value) {
            return switch (value) {
                case "cursor" -> CURSOR;
                case "paging" -> PAGING;
                default -> throw new IllegalArgumentException("--reader must be cursor or paging");
            };
        }
    }

    private record DatabaseConfig(String url, String user, String password) {}

    private record SourceRow(long customerId, String fullName, boolean active, long balanceCents) {}

    private record ProjectedRow(
            String importName,
            String sourceDigest,
            long customerId,
            String displayName,
            long loyaltyScore,
            boolean premium,
            byte[] fingerprint) {}

    private record ExecutionIdentity(
            String importName, String sourceDigest, ReaderMode readerMode, String definitionRevision) {}

    private record Checkpoint(Long lastCustomerId, long committedChunks, long committedRows) {}

    private static final class RunState {
        private Long lastCustomerId;
        private long committedChunks;
        private long committedRows;
        private final List<ProjectedRow> chunk;

        RunState(Checkpoint checkpoint, int chunkSize) {
            this.lastCustomerId = checkpoint.lastCustomerId();
            this.committedChunks = checkpoint.committedChunks();
            this.committedRows = checkpoint.committedRows();
            this.chunk = new ArrayList<>(chunkSize);
        }
    }

    private record RunConfig(
            String importName,
            ReaderMode readerMode,
            int chunkSize,
            int readBatchSize,
            Long failAfterChunk) {
        String definitionRevision() {
            return readerMode == ReaderMode.CURSOR ? CURSOR_DEFINITION_REVISION : PAGING_DEFINITION_REVISION;
        }

        static RunConfig parse(Options options) {
            options.requireOnly("import-name", "reader", "chunk-size", "fetch-size", "page-size", "fail-after-chunk");
            String importName = required(options, "import-name");
            ReaderMode reader = ReaderMode.parse(required(options, "reader"));
            int chunkSize = positiveInt(options, "chunk-size", DEFAULT_CHUNK_SIZE, MAX_CHUNK_SIZE);
            boolean hasFetch = options.values().containsKey("fetch-size");
            boolean hasPage = options.values().containsKey("page-size");
            if (reader == ReaderMode.CURSOR && hasPage) {
                throw new IllegalArgumentException("--page-size is only valid with --reader paging");
            }
            if (reader == ReaderMode.PAGING && hasFetch) {
                throw new IllegalArgumentException("--fetch-size is only valid with --reader cursor");
            }
            int readBatchSize = reader == ReaderMode.CURSOR
                    ? positiveInt(options, "fetch-size", DEFAULT_FETCH_SIZE, MAX_READ_BATCH_SIZE)
                    : positiveInt(options, "page-size", DEFAULT_PAGE_SIZE, MAX_READ_BATCH_SIZE);
            return new RunConfig(importName, reader, chunkSize, readBatchSize, optionalPositiveLong(options, "fail-after-chunk"));
        }
    }

    private record Options(java.util.Map<String, String> values) {
        void requireOnly(String... allowed) {
            Set<String> allowedSet = Set.of(allowed);
            for (String key : values.keySet()) {
                if (!allowedSet.contains(key)) {
                    throw new IllegalArgumentException("unknown option --" + key);
                }
            }
        }

        static Options parse(String[] args, int start) {
            java.util.Map<String, String> values = new java.util.LinkedHashMap<>();
            for (int index = start; index < args.length; index += 2) {
                String key = args[index];
                if (!key.startsWith("--") || index + 1 >= args.length) {
                    throw new IllegalArgumentException("options must be supplied as --key value pairs");
                }
                String normalized = key.substring(2);
                if (values.putIfAbsent(normalized, args[index + 1]) != null) {
                    throw new IllegalArgumentException("duplicate option --" + normalized);
                }
            }
            return new Options(java.util.Map.copyOf(values));
        }
    }

    private record Cli(String command, Options options, DatabaseConfig database) {
        static Cli parse(String[] args) {
            if (args.length < 1) {
                throw new IllegalArgumentException("usage: raw-jdbc <migrate|run|inspect> [--key value ...]");
            }
            String command = args[0];
            Options options = Options.parse(args, 1);
            DatabaseConfig database = new DatabaseConfig(
                    env("RAW_JDBC_DATABASE_URL", "jdbc:postgresql://localhost:5434/postgres_postgres_workload"),
                    env("RAW_JDBC_DATABASE_USER", "oxide_batch_workload"),
                    env("RAW_JDBC_DATABASE_PASSWORD", "oxide_batch_workload"));
            return new Cli(command, options, database);
        }
    }

    private static String env(String name, String defaultValue) {
        String value = System.getenv(name);
        return value == null || value.isEmpty() ? defaultValue : value;
    }

    private static final class InjectedFailure extends Exception {
        private static final long serialVersionUID = 1L;

        InjectedFailure(String message) {
            super(message);
        }
    }
}
