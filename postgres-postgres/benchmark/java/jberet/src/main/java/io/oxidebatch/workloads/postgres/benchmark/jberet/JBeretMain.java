package io.oxidebatch.workloads.postgres.benchmark.jberet;

import java.io.Serializable;
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
import java.time.Duration;
import java.util.HexFormat;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Properties;
import java.util.Set;

import jakarta.batch.api.BatchProperty;
import jakarta.batch.api.chunk.ItemProcessor;
import jakarta.batch.api.chunk.ItemReader;
import jakarta.batch.api.chunk.ItemWriter;
import jakarta.batch.operations.JobOperator;
import jakarta.batch.runtime.BatchRuntime;
import jakarta.batch.runtime.BatchStatus;
import jakarta.batch.runtime.JobExecution;
import jakarta.inject.Inject;

/**
 * JBeret 3.2.0.Final clean semantic-qualification candidate for campaign #86.
 *
 * <p>PR1 intentionally proves only clean cursor/paging behavior. The writer commits its business
 * connection once per chunk because JBeret Java SE's LocalTransactionManager does not enlist JDBC
 * resources. JBeret repository/checkpoint atomicity is therefore deliberately unclaimed here and is
 * a hard proof obligation for PR2 before this candidate can enter the primary comparison class.
 */
public final class JBeretMain {
    private static final String JBERET_VERSION = "3.2.0.Final";
    private static final String JOB_XML_NAME = "postgres-postgres";
    private static final String PROVISIONAL_WRITER_COMMIT_MODEL = "writer-local-commit";
    private static final int DEFAULT_FETCH_SIZE = 500;
    private static final int DEFAULT_PAGE_SIZE = 750;
    private static final int DEFAULT_CHUNK_SIZE = 1_000;
    private static final int DEFAULT_MAX_BIND_PARAMS = 2_000;
    private static final int COLUMNS_PER_ROW = 7;
    private static final int MAX_PARAMETERS_PER_STATEMENT = 2_000;
    private static final int ROWS_PER_STATEMENT = MAX_PARAMETERS_PER_STATEMENT / COLUMNS_PER_ROW;
    private static final int MAX_BOUND_PARAMETERS = ROWS_PER_STATEMENT * COLUMNS_PER_ROW;
    private static final int MAX_CHUNK_SIZE = 1_000_000;
    private static final int MAX_READ_BATCH_SIZE = 1_000_000;
    private static final int FINGERPRINT_LEN = 16;
    private static final long PREMIUM_THRESHOLD_CENTS = 50_000L;
    private static final Duration JOB_TIMEOUT = Duration.ofMinutes(5);
    private static final Set<String> EXPECTED_REPOSITORY_TABLES = Set.of(
            "job_instance", "job_execution", "step_execution", "partition_execution");

    private JBeretMain() {}

    public static void main(String[] args) throws Exception {
        Cli cli = Cli.parse(args);
        DatabaseConfig database = DatabaseConfig.fromEnvironment();
        switch (cli.command()) {
            case "migrate" -> migrate(database);
            case "run" -> run(database, RunConfig.parse(cli.options()));
            default -> throw new IllegalArgumentException("unknown command: " + cli.command());
        }
    }

    private static void migrate(DatabaseConfig database) throws Exception {
        try (Connection connection = connect(database); Statement statement = connection.createStatement()) {
            statement.execute("CREATE SCHEMA IF NOT EXISTS jberet");
        }

        JobOperator operator = BatchRuntime.getJobOperator();
        operator.getJobNames();
        assertRepositorySchemaComplete(database);
    }

    private static void assertRepositorySchemaComplete(DatabaseConfig database) throws SQLException {
        String sql = "SELECT table_name FROM information_schema.tables "
                + "WHERE table_schema = 'jberet' ORDER BY table_name";
        java.util.HashSet<String> tables = new java.util.HashSet<>();
        try (Connection connection = connect(database);
                PreparedStatement statement = connection.prepareStatement(sql);
                ResultSet rows = statement.executeQuery()) {
            while (rows.next()) {
                tables.add(rows.getString(1));
            }
        }
        if (!tables.equals(EXPECTED_REPOSITORY_TABLES)) {
            throw new IllegalStateException(
                    "unexpected JBeret repository table inventory: expected="
                            + EXPECTED_REPOSITORY_TABLES
                            + " actual="
                            + tables);
        }
    }

    private static void run(DatabaseConfig database, RunConfig config) throws Exception {
        try (Connection sourceLock = connect(database)) {
            sourceLock.setAutoCommit(false);
            try (Statement lock = sourceLock.createStatement()) {
                lock.execute("LOCK TABLE app_source.source_customer IN SHARE MODE");
            }

            String sourceDigest = sourceDigest(sourceLock);
            Properties parameters = new Properties();
            parameters.setProperty("importName", config.importName());
            parameters.setProperty("sourceDigest", sourceDigest);
            parameters.setProperty("readerMode", config.readerMode().value);
            parameters.setProperty("chunkSize", Integer.toString(config.chunkSize()));
            parameters.setProperty("fetchSize", Integer.toString(config.fetchSize()));
            parameters.setProperty("pageSize", Integer.toString(config.pageSize()));
            parameters.setProperty("maxBindParams", Integer.toString(DEFAULT_MAX_BIND_PARAMS));
            parameters.setProperty("definitionRevision", config.definitionRevision());

            JobOperator operator = BatchRuntime.getJobOperator();
            long executionId = operator.start(JOB_XML_NAME, parameters);
            JobExecution execution = awaitTerminal(operator, executionId);
            if (execution.getBatchStatus() != BatchStatus.COMPLETED) {
                throw new IllegalStateException(
                        "JBeret execution did not complete: id="
                                + executionId
                                + " status="
                                + execution.getBatchStatus()
                                + " exitStatus="
                                + execution.getExitStatus());
            }

            long resultRows = countResultRows(database, config.importName(), sourceDigest);
            System.out.printf(
                    Locale.ROOT,
                    "jberet_version=%s source_digest=%s reader_mode=%s execution_id=%d status=%s result_rows=%d business_commit_model=%s%n",
                    JBERET_VERSION,
                    sourceDigest,
                    config.readerMode().value,
                    executionId,
                    execution.getBatchStatus(),
                    resultRows,
                    PROVISIONAL_WRITER_COMMIT_MODEL);
        }
    }

    private static JobExecution awaitTerminal(JobOperator operator, long executionId) throws InterruptedException {
        long deadline = System.nanoTime() + JOB_TIMEOUT.toNanos();
        while (true) {
            JobExecution execution = operator.getJobExecution(executionId);
            BatchStatus status = execution.getBatchStatus();
            if (status != BatchStatus.STARTING && status != BatchStatus.STARTED && status != BatchStatus.STOPPING) {
                return execution;
            }
            if (System.nanoTime() >= deadline) {
                throw new IllegalStateException(
                        "timed out waiting for JBeret execution " + executionId + " in status " + status);
            }
            Thread.sleep(25L);
        }
    }

    private static long countResultRows(DatabaseConfig database, String importName, String sourceDigest)
            throws SQLException {
        try (Connection connection = connect(database);
                PreparedStatement statement = connection.prepareStatement(
                        "SELECT count(*) FROM app_business.customer_projection "
                                + "WHERE import_name = ? AND source_digest = ?")) {
            statement.setString(1, importName);
            statement.setString(2, sourceDigest);
            try (ResultSet rows = statement.executeQuery()) {
                if (!rows.next()) {
                    throw new IllegalStateException("result row-count query returned no row");
                }
                return rows.getLong(1);
            }
        }
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

    private static ProjectedRow project(String importName, String sourceDigest, SourceRow source)
            throws NoSuchAlgorithmException {
        if (source.fullName().trim().isEmpty()) {
            throw new IllegalArgumentException(
                    "source full_name must not be empty for customer " + source.customerId());
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
                importName,
                sourceDigest,
                source.customerId(),
                source.fullName().toUpperCase(Locale.ROOT),
                source.balanceCents() / 100L,
                source.balanceCents() >= PREMIUM_THRESHOLD_CENTS,
                fingerprint);
    }

    private static byte[] littleEndian(long value) {
        return ByteBuffer.allocate(Long.BYTES).order(ByteOrder.LITTLE_ENDIAN).putLong(value).array();
    }

    private static Connection connect(DatabaseConfig database) throws SQLException {
        if (database.url().toLowerCase(Locale.ROOT).contains("rewritebatchedinserts=true")) {
            throw new IllegalArgumentException("reWriteBatchedInserts=true is forbidden for the parity candidate");
        }
        Properties properties = new Properties();
        properties.setProperty("user", database.user());
        properties.setProperty("password", database.password());
        properties.setProperty("ApplicationName", "oxide-batch-workloads-jberet");
        properties.setProperty("reWriteBatchedInserts", "false");
        return DriverManager.getConnection(database.url(), properties);
    }

    public static final class PostgresReader implements ItemReader {
        @Inject
        @BatchProperty(name = "readerMode")
        private String readerMode;

        @Inject
        @BatchProperty(name = "fetchSize")
        private String fetchSize;

        @Inject
        @BatchProperty(name = "pageSize")
        private String pageSize;

        private Connection connection;
        private PreparedStatement cursorStatement;
        private ResultSet currentRows;
        private ReaderMode mode;
        private int effectiveFetchSize;
        private int effectivePageSize;
        private long lastCustomerId;

        public PostgresReader() {}

        @Override
        public void open(Serializable checkpoint) throws Exception {
            mode = ReaderMode.parse(readerMode);
            effectiveFetchSize = positiveInt(fetchSize, "fetchSize", MAX_READ_BATCH_SIZE);
            effectivePageSize = positiveInt(pageSize, "pageSize", MAX_READ_BATCH_SIZE);
            if (checkpoint == null) {
                lastCustomerId = 0L;
            } else if (checkpoint instanceof Long value && value >= 0L) {
                lastCustomerId = value;
            } else {
                throw new IllegalArgumentException("unexpected JBeret reader checkpoint: " + checkpoint);
            }

            connection = connect(DatabaseConfig.fromEnvironment());
            connection.setAutoCommit(false);
            connection.setReadOnly(true);
            if (mode == ReaderMode.CURSOR) {
                cursorStatement = connection.prepareStatement(
                        "SELECT customer_id, full_name, is_active, balance_cents "
                                + "FROM app_source.source_customer WHERE customer_id > ? ORDER BY customer_id",
                        ResultSet.TYPE_FORWARD_ONLY,
                        ResultSet.CONCUR_READ_ONLY);
                cursorStatement.setLong(1, lastCustomerId);
                cursorStatement.setFetchSize(effectiveFetchSize);
                currentRows = cursorStatement.executeQuery();
            }
        }

        @Override
        public Object readItem() throws Exception {
            if (mode == ReaderMode.CURSOR) {
                if (!currentRows.next()) {
                    return null;
                }
                return consumeCurrentRow();
            }
            while (true) {
                if (currentRows != null && currentRows.next()) {
                    return consumeCurrentRow();
                }
                closeCurrentPage();
                PreparedStatement page = connection.prepareStatement(
                        "SELECT customer_id, full_name, is_active, balance_cents "
                                + "FROM app_source.source_customer WHERE customer_id > ? "
                                + "ORDER BY customer_id LIMIT ?",
                        ResultSet.TYPE_FORWARD_ONLY,
                        ResultSet.CONCUR_READ_ONLY);
                page.setLong(1, lastCustomerId);
                page.setInt(2, effectivePageSize);
                page.setFetchSize(effectivePageSize);
                cursorStatement = page;
                currentRows = page.executeQuery();
                if (!currentRows.next()) {
                    closeCurrentPage();
                    return null;
                }
                return consumeCurrentRow();
            }
        }

        private SourceRow consumeCurrentRow() throws SQLException {
            SourceRow source = new SourceRow(
                    currentRows.getLong("customer_id"),
                    currentRows.getString("full_name"),
                    currentRows.getBoolean("is_active"),
                    currentRows.getLong("balance_cents"));
            if (source.customerId() <= lastCustomerId) {
                throw new IllegalStateException(
                        "reader ordering regressed: previous=" + lastCustomerId + " current=" + source.customerId());
            }
            lastCustomerId = source.customerId();
            return source;
        }

        @Override
        public Serializable checkpointInfo() {
            return lastCustomerId;
        }

        @Override
        public void close() throws Exception {
            closeCurrentPage();
            if (connection != null) {
                connection.close();
                connection = null;
            }
        }

        private void closeCurrentPage() throws SQLException {
            if (currentRows != null) {
                currentRows.close();
                currentRows = null;
            }
            if (cursorStatement != null) {
                cursorStatement.close();
                cursorStatement = null;
            }
        }
    }

    public static final class PostgresProcessor implements ItemProcessor {
        @Inject
        @BatchProperty(name = "importName")
        private String importName;

        @Inject
        @BatchProperty(name = "sourceDigest")
        private String sourceDigest;

        public PostgresProcessor() {}

        @Override
        public Object processItem(Object item) throws Exception {
            if (!(item instanceof SourceRow source)) {
                throw new IllegalArgumentException("unexpected JBeret reader item: " + item);
            }
            return project(importName, sourceDigest, source);
        }
    }

    public static final class PostgresWriter implements ItemWriter {
        @Inject
        @BatchProperty(name = "maxBindParams")
        private String maxBindParams;

        private Connection connection;
        private int rowsPerStatement;
        private int maxBoundParameters;

        public PostgresWriter() {}

        @Override
        public void open(Serializable checkpoint) throws Exception {
            if (checkpoint != null) {
                throw new IllegalArgumentException("JBeret writer does not define checkpoint state in PR1");
            }
            int configuredMax = positiveInt(maxBindParams, "maxBindParams", MAX_PARAMETERS_PER_STATEMENT);
            if (configuredMax != MAX_PARAMETERS_PER_STATEMENT) {
                throw new IllegalArgumentException(
                        "JBeret parity writer requires maxBindParams=" + MAX_PARAMETERS_PER_STATEMENT);
            }
            rowsPerStatement = configuredMax / COLUMNS_PER_ROW;
            maxBoundParameters = rowsPerStatement * COLUMNS_PER_ROW;
            if (rowsPerStatement != ROWS_PER_STATEMENT || maxBoundParameters != MAX_BOUND_PARAMETERS) {
                throw new IllegalStateException("JBeret writer parity arithmetic drifted");
            }
            connection = connect(DatabaseConfig.fromEnvironment());
            connection.setAutoCommit(false);
        }

        @Override
        public void writeItems(List<Object> items) throws Exception {
            if (items.isEmpty()) {
                return;
            }
            try {
                for (int start = 0; start < items.size(); start += rowsPerStatement) {
                    int end = Math.min(start + rowsPerStatement, items.size());
                    insertMultiValues(items.subList(start, end));
                }
                connection.commit();
            } catch (Exception error) {
                connection.rollback();
                throw error;
            }
        }

        private void insertMultiValues(List<Object> rows) throws SQLException {
            int boundParameters = Math.multiplyExact(rows.size(), COLUMNS_PER_ROW);
            if (rows.isEmpty() || rows.size() > rowsPerStatement || boundParameters > maxBoundParameters) {
                throw new IllegalArgumentException(
                        "writer batch exceeds parity bound of "
                                + rowsPerStatement
                                + " rows / "
                                + maxBoundParameters
                                + " parameters");
            }

            StringBuilder sql = new StringBuilder(
                    "INSERT INTO app_business.customer_projection "
                            + "(import_name, source_digest, customer_id, display_name, loyalty_score, is_premium, row_fingerprint) VALUES ");
            for (int index = 0; index < rows.size(); index++) {
                if (index != 0) {
                    sql.append(',');
                }
                sql.append("(?,?,?,?,?,?,?)");
            }

            try (PreparedStatement statement = connection.prepareStatement(sql.toString())) {
                int parameter = 1;
                for (Object item : rows) {
                    if (!(item instanceof ProjectedRow row)) {
                        throw new IllegalArgumentException("unexpected JBeret writer item: " + item);
                    }
                    statement.setString(parameter++, row.importName());
                    statement.setString(parameter++, row.sourceDigest());
                    statement.setLong(parameter++, row.customerId());
                    statement.setString(parameter++, row.displayName());
                    statement.setLong(parameter++, row.loyaltyScore());
                    statement.setBoolean(parameter++, row.premium());
                    statement.setBytes(parameter++, row.fingerprint());
                }
                int affected = statement.executeUpdate();
                if (affected != rows.size()) {
                    throw new IllegalStateException(
                            "writer affected " + affected + " rows for expected batch size " + rows.size());
                }
            }
        }

        @Override
        public Serializable checkpointInfo() {
            return null;
        }

        @Override
        public void close() throws Exception {
            if (connection != null) {
                connection.close();
                connection = null;
            }
        }
    }

    private static int positiveInt(String value, String field, int maximum) {
        int parsed = Integer.parseInt(value);
        if (parsed < 1 || parsed > maximum) {
            throw new IllegalArgumentException(field + " must be between 1 and " + maximum);
        }
        return parsed;
    }

    private static String requiredEnv(String name) {
        String value = System.getenv(name);
        if (value == null || value.isBlank()) {
            throw new IllegalStateException("required environment variable is missing: " + name);
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

    private record DatabaseConfig(String url, String user, String password) {
        static DatabaseConfig fromEnvironment() {
            return new DatabaseConfig(
                    requiredEnv("JBERET_DATABASE_URL"),
                    requiredEnv("JBERET_DATABASE_USER"),
                    requiredEnv("JBERET_DATABASE_PASSWORD"));
        }
    }

    private record SourceRow(long customerId, String fullName, boolean active, long balanceCents) {}

    private record ProjectedRow(
            String importName,
            String sourceDigest,
            long customerId,
            String displayName,
            long loyaltyScore,
            boolean premium,
            byte[] fingerprint) {}

    private record RunConfig(
            String importName,
            ReaderMode readerMode,
            int chunkSize,
            int fetchSize,
            int pageSize) {
        String definitionRevision() {
            return readerMode == ReaderMode.CURSOR
                    ? "jberet-postgres-postgres-cursor-v1"
                    : "jberet-postgres-postgres-paging-v1";
        }

        static RunConfig parse(Map<String, String> options) {
            String importName = required(options, "import-name");
            ReaderMode reader = ReaderMode.parse(required(options, "reader"));
            int chunkSize = positiveInt(
                    options.getOrDefault("chunk-size", Integer.toString(DEFAULT_CHUNK_SIZE)),
                    "chunk-size",
                    MAX_CHUNK_SIZE);
            boolean hasFetch = options.containsKey("fetch-size");
            boolean hasPage = options.containsKey("page-size");
            if (reader == ReaderMode.CURSOR && hasPage) {
                throw new IllegalArgumentException("--page-size is only valid with --reader paging");
            }
            if (reader == ReaderMode.PAGING && hasFetch) {
                throw new IllegalArgumentException("--fetch-size is only valid with --reader cursor");
            }
            int fetch = positiveInt(
                    options.getOrDefault("fetch-size", Integer.toString(DEFAULT_FETCH_SIZE)),
                    "fetch-size",
                    MAX_READ_BATCH_SIZE);
            int page = positiveInt(
                    options.getOrDefault("page-size", Integer.toString(DEFAULT_PAGE_SIZE)),
                    "page-size",
                    MAX_READ_BATCH_SIZE);
            return new RunConfig(importName, reader, chunkSize, fetch, page);
        }
    }

    private record Cli(String command, Map<String, String> options) {
        private static final Set<String> ALLOWED = Set.of(
                "import-name", "reader", "chunk-size", "fetch-size", "page-size");

        static Cli parse(String[] args) {
            if (args.length == 0) {
                throw new IllegalArgumentException("usage: JBeretMain <migrate|run> [options]");
            }
            String command = args[0];
            if ("migrate".equals(command)) {
                if (args.length != 1) {
                    throw new IllegalArgumentException("migrate does not accept command-line options");
                }
                return new Cli(command, Map.of());
            }
            if (!"run".equals(command)) {
                throw new IllegalArgumentException("unknown command: " + command);
            }
            java.util.LinkedHashMap<String, String> values = new java.util.LinkedHashMap<>();
            for (int index = 1; index < args.length; index += 2) {
                if (!args[index].startsWith("--") || index + 1 >= args.length) {
                    throw new IllegalArgumentException("options must be --name value pairs");
                }
                String key = args[index].substring(2);
                if (!ALLOWED.contains(key)) {
                    throw new IllegalArgumentException("unsupported option --" + key);
                }
                if (values.putIfAbsent(key, args[index + 1]) != null) {
                    throw new IllegalArgumentException("duplicate option --" + key);
                }
            }
            return new Cli(command, Map.copyOf(values));
        }
    }

    private static String required(Map<String, String> options, String key) {
        String value = options.get(key);
        if (value == null || value.isEmpty()) {
            throw new IllegalArgumentException("missing required option --" + key);
        }
        return value;
    }
}
