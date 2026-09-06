package io.oxidebatch.workloads.postgres.benchmark.springbatch;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Properties;
import java.util.concurrent.CountDownLatch;

import javax.sql.DataSource;

import org.springframework.batch.core.BatchStatus;
import org.springframework.batch.core.SpringBatchVersion;
import org.springframework.batch.core.configuration.support.MapJobRegistry;
import org.springframework.batch.core.job.Job;
import org.springframework.batch.core.job.JobExecution;
import org.springframework.batch.core.job.JobInstance;
import org.springframework.batch.core.job.builder.JobBuilder;
import org.springframework.batch.core.job.parameters.JobParameters;
import org.springframework.batch.core.job.parameters.JobParametersBuilder;
import org.springframework.batch.core.launch.JobOperator;
import org.springframework.batch.core.launch.support.TaskExecutorJobOperator;
import org.springframework.batch.core.repository.JobRepository;
import org.springframework.batch.core.repository.support.JdbcJobRepositoryFactoryBean;
import org.springframework.batch.core.step.Step;
import org.springframework.batch.core.step.builder.StepBuilder;
import org.springframework.batch.infrastructure.item.Chunk;
import org.springframework.batch.infrastructure.item.ItemProcessor;
import org.springframework.batch.infrastructure.item.ItemReader;
import org.springframework.batch.infrastructure.item.ItemWriter;
import org.springframework.batch.infrastructure.item.database.Order;
import org.springframework.batch.infrastructure.item.database.builder.JdbcCursorItemReaderBuilder;
import org.springframework.batch.infrastructure.item.database.builder.JdbcPagingItemReaderBuilder;
import org.springframework.batch.infrastructure.item.database.support.PostgresPagingQueryProvider;
import org.springframework.core.io.ClassPathResource;
import org.springframework.core.task.SyncTaskExecutor;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.jdbc.datasource.init.ResourceDatabasePopulator;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;

/**
 * Spring Batch 6.0.5 semantic-parity candidate for campaign #79.
 *
 * <p>The candidate uses Spring Batch public APIs and a real JDBC JobRepository. The source digest is
 * computed while an explicit PostgreSQL SHARE lock is held; that lock remains held for the whole
 * synchronous job execution. Business writes run through JdbcTemplate on the same DataSource and
 * transaction manager as Spring Batch metadata, so the writer never owns a private commit boundary.
 */
public final class SpringBatchMain {
    private static final String SPRING_BATCH_VERSION = "6.0.5";
    private static final String JOB_NAME = "spring-postgres-postgres";
    private static final String STEP_NAME = "spring-postgres-postgres-step";
    private static final String CURSOR_DEFINITION_REVISION = "spring-batch-postgres-postgres-cursor-v1";
    private static final String PAGING_DEFINITION_REVISION = "spring-batch-postgres-postgres-paging-v1";
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
    private static final int HISTORY_PAGE_SIZE = 100;
    private static final long PREMIUM_THRESHOLD_CENTS = 50_000L;
    private static final String BATCH_SCHEMA_RESOURCE = "org/springframework/batch/core/schema-postgresql.sql";
    private static final int EXPECTED_BATCH_TABLES = 6;
    private static final int EXPECTED_BATCH_SEQUENCES = 3;
    private static final String BATCH_TABLE_INVENTORY_SQL =
            "SELECT count(*) FROM information_schema.tables "
                    + "WHERE table_schema = 'spring_batch' AND table_name IN ("
                    + "'batch_job_instance','batch_job_execution','batch_job_execution_params',"
                    + "'batch_step_execution','batch_step_execution_context','batch_job_execution_context')";
    private static final String BATCH_SEQUENCE_INVENTORY_SQL =
            "SELECT count(*) FROM information_schema.sequences "
                    + "WHERE sequence_schema = 'spring_batch' AND sequence_name IN ("
                    + "'batch_step_execution_seq','batch_job_execution_seq','batch_job_instance_seq')";

    private SpringBatchMain() {}

    public static void main(String[] args) throws Exception {
        verifySpringBatchVersion();
        Cli cli = Cli.parse(args);
        switch (cli.command()) {
            case "migrate" -> migrate(cli.database());
            case "run" -> run(cli.database(), RunConfig.parse(cli.options()));
            case "recover" -> recover(cli.database(), RunConfig.parseForRecover(cli.options()));
            default -> throw new IllegalArgumentException("unknown command: " + cli.command());
        }
    }

    private static void verifySpringBatchVersion() {
        String actual = SpringBatchVersion.getVersion();
        if (!SPRING_BATCH_VERSION.equals(actual)) {
            throw new IllegalStateException(
                    "expected Spring Batch " + SPRING_BATCH_VERSION + ", got " + actual);
        }
    }

    private static void migrate(DatabaseConfig database) throws Exception {
        try (Connection connection = connectBase(database); Statement statement = connection.createStatement()) {
            statement.execute("CREATE SCHEMA IF NOT EXISTS spring_batch");
        }

        DataSource dataSource = springDataSource(database);
        JdbcTemplate jdbc = new JdbcTemplate(dataSource);
        int existingTables = countSchemaObjects(jdbc, BATCH_TABLE_INVENTORY_SQL, "tables");
        int existingSequences = countSchemaObjects(jdbc, BATCH_SEQUENCE_INVENTORY_SQL, "sequences");
        if (existingTables == EXPECTED_BATCH_TABLES && existingSequences == EXPECTED_BATCH_SEQUENCES) {
            return;
        }
        if (existingTables != 0 || existingSequences != 0) {
            throw new IllegalStateException(
                    "partial Spring Batch metadata schema detected: tables="
                            + existingTables
                            + "/"
                            + EXPECTED_BATCH_TABLES
                            + " sequences="
                            + existingSequences
                            + "/"
                            + EXPECTED_BATCH_SEQUENCES);
        }

        ResourceDatabasePopulator populator = new ResourceDatabasePopulator();
        populator.addScript(new ClassPathResource(BATCH_SCHEMA_RESOURCE));
        populator.execute(dataSource);
        assertBatchSchemaComplete(jdbc);
    }

    private static int countSchemaObjects(JdbcTemplate jdbc, String sql, String objectType) {
        Integer count = jdbc.queryForObject(sql, Integer.class);
        if (count == null) {
            throw new IllegalStateException("Spring Batch " + objectType + " inventory query returned null");
        }
        return count;
    }

    private static void assertBatchSchemaComplete(JdbcTemplate jdbc) {
        int tables = countSchemaObjects(jdbc, BATCH_TABLE_INVENTORY_SQL, "tables");
        int sequences = countSchemaObjects(jdbc, BATCH_SEQUENCE_INVENTORY_SQL, "sequences");
        if (tables != EXPECTED_BATCH_TABLES || sequences != EXPECTED_BATCH_SEQUENCES) {
            throw new IllegalStateException(
                    "Spring Batch metadata schema initialization incomplete: tables="
                            + tables
                            + "/"
                            + EXPECTED_BATCH_TABLES
                            + " sequences="
                            + sequences
                            + "/"
                            + EXPECTED_BATCH_SEQUENCES);
        }
    }

    private static void run(DatabaseConfig database, RunConfig config) throws Exception {
        try (Connection sourceLock = connectBase(database)) {
            sourceLock.setAutoCommit(false);
            try (Statement lock = sourceLock.createStatement()) {
                lock.execute("LOCK TABLE app_source.source_customer IN SHARE MODE");
            }

            String sourceDigest = sourceDigest(sourceLock);
            DataSource dataSource = springDataSource(database);
            DataSourceTransactionManager transactionManager = new DataSourceTransactionManager(dataSource);
            JobRepository jobRepository = jobRepository(dataSource, transactionManager);

            assertLogicalSourceCompatible(jobRepository, config, sourceDigest);

            JobParameters parameters = jobParameters(config, sourceDigest);
            Job job = buildJob(jobRepository, transactionManager, dataSource, config, sourceDigest);
            JobOperator operator = jobOperator(jobRepository, job);

            JobExecution execution = launchOrRestart(operator, jobRepository, job, parameters);
            System.out.printf(
                    Locale.ROOT,
                    "source_digest=%s reader_mode=%s execution_id=%d status=%s%n",
                    sourceDigest,
                    config.readerMode().value,
                    execution.getId(),
                    execution.getStatus());
            if (execution.getStatus() != BatchStatus.COMPLETED) {
                throw new IllegalStateException(
                        "Spring Batch execution did not complete: id=" + execution.getId()
                                + " status=" + execution.getStatus()
                                + " failures=" + execution.getAllFailureExceptions());
            }
        }
    }

    private static void recover(DatabaseConfig database, RunConfig config) throws Exception {
        try (Connection sourceLock = connectBase(database)) {
            sourceLock.setAutoCommit(false);
            try (Statement lock = sourceLock.createStatement()) {
                lock.execute("LOCK TABLE app_source.source_customer IN SHARE MODE");
            }

            String sourceDigest = sourceDigest(sourceLock);
            DataSource dataSource = springDataSource(database);
            DataSourceTransactionManager transactionManager = new DataSourceTransactionManager(dataSource);
            JobRepository jobRepository = jobRepository(dataSource, transactionManager);

            assertLogicalSourceCompatible(jobRepository, config, sourceDigest);
            JobExecution execution = latestLogicalExecution(jobRepository, config, sourceDigest);
            if (execution == null) {
                throw new IllegalStateException("no Spring Batch execution exists for requested logical identity");
            }
            if (execution.getStatus() != BatchStatus.STARTED) {
                throw new IllegalStateException(
                        "Spring Batch execution is not recoverable: execution_id=" + execution.getId()
                                + " status=" + execution.getStatus());
            }

            Job job = buildJob(jobRepository, transactionManager, dataSource, config, sourceDigest);
            JobOperator operator = jobOperator(jobRepository, job);
            JobExecution recovered = operator.recover(execution);
            System.out.printf(
                    Locale.ROOT,
                    "source_digest=%s reader_mode=%s execution_id=%d recovered_status=%s%n",
                    sourceDigest,
                    config.readerMode().value,
                    recovered.getId(),
                    recovered.getStatus());
            if (recovered.getStatus() != BatchStatus.FAILED) {
                throw new IllegalStateException(
                        "Spring Batch recovery did not mark execution FAILED: id=" + recovered.getId()
                                + " status=" + recovered.getStatus());
            }
        }
    }

    private static void assertLogicalSourceCompatible(
            JobRepository jobRepository, RunConfig config, String currentSourceDigest) {
        int start = 0;
        while (true) {
            List<JobInstance> instances = jobRepository.getJobInstances(JOB_NAME, start, HISTORY_PAGE_SIZE);
            if (instances.isEmpty()) {
                return;
            }
            for (JobInstance instance : instances) {
                JobExecution execution = jobRepository.getLastJobExecution(instance);
                if (execution == null || !sameLogicalIdentity(execution.getJobParameters(), config)) {
                    continue;
                }
                String priorDigest = execution.getJobParameters().getString("source_digest");
                if (priorDigest == null) {
                    throw new IllegalStateException(
                            "existing logical Spring execution lacks source_digest: instance_id=" + instance.getInstanceId());
                }
                if (!priorDigest.equals(currentSourceDigest)) {
                    throw new IllegalStateException(
                            "source digest changed for existing logical Spring execution: instance_id="
                                    + instance.getInstanceId()
                                    + " prior=" + priorDigest
                                    + " current=" + currentSourceDigest);
                }
            }
            if (instances.size() < HISTORY_PAGE_SIZE) {
                return;
            }
            start = Math.addExact(start, instances.size());
        }
    }

    private static JobExecution latestLogicalExecution(
            JobRepository jobRepository, RunConfig config, String sourceDigest) {
        JobExecution latest = null;
        int start = 0;
        while (true) {
            List<JobInstance> instances = jobRepository.getJobInstances(JOB_NAME, start, HISTORY_PAGE_SIZE);
            if (instances.isEmpty()) {
                return latest;
            }
            for (JobInstance instance : instances) {
                JobExecution execution = jobRepository.getLastJobExecution(instance);
                if (execution == null
                        || !sameLogicalIdentity(execution.getJobParameters(), config)
                        || !sourceDigest.equals(execution.getJobParameters().getString("source_digest"))) {
                    continue;
                }
                if (latest == null || execution.getId() > latest.getId()) {
                    latest = execution;
                }
            }
            if (instances.size() < HISTORY_PAGE_SIZE) {
                return latest;
            }
            start = Math.addExact(start, instances.size());
        }
    }

    private static boolean sameLogicalIdentity(JobParameters parameters, RunConfig config) {
        return config.importName().equals(parameters.getString("import_name"))
                && config.readerMode().value.equals(parameters.getString("reader_mode"))
                && config.definitionRevision().equals(parameters.getString("definition_revision"));
    }

    private static JobExecution launchOrRestart(
            JobOperator operator, JobRepository jobRepository, Job job, JobParameters parameters) throws Exception {
        JobInstance instance = jobRepository.getJobInstance(JOB_NAME, parameters);
        if (instance == null) {
            return operator.start(job, parameters);
        }

        List<JobExecution> executions = new ArrayList<>(jobRepository.getJobExecutions(instance));
        JobExecution last = executions.stream()
                .max(Comparator.comparingLong(JobExecution::getId))
                .orElseThrow(() -> new IllegalStateException("job instance exists without executions"));
        return switch (last.getStatus()) {
            case FAILED, STOPPED -> operator.restart(last);
            case COMPLETED -> throw new IllegalStateException(
                    "Spring Batch job instance is already complete: execution_id=" + last.getId());
            default -> throw new IllegalStateException(
                    "Spring Batch job instance is not restartable without recovery: execution_id="
                            + last.getId() + " status=" + last.getStatus());
        };
    }

    private static JobRepository jobRepository(
            DataSource dataSource, DataSourceTransactionManager transactionManager) throws Exception {
        JdbcJobRepositoryFactoryBean factory = new JdbcJobRepositoryFactoryBean();
        factory.setDataSource(dataSource);
        factory.setTransactionManager(transactionManager);
        factory.setDatabaseType("POSTGRES");
        factory.setTablePrefix("spring_batch.BATCH_");
        factory.afterPropertiesSet();
        return factory.getObject();
    }

    private static JobOperator jobOperator(JobRepository jobRepository, Job job) throws Exception {
        MapJobRegistry registry = new MapJobRegistry();
        registry.register(job);

        TaskExecutorJobOperator operator = new TaskExecutorJobOperator();
        operator.setJobRepository(jobRepository);
        operator.setJobRegistry(registry);
        operator.setTaskExecutor(new SyncTaskExecutor());
        operator.afterPropertiesSet();
        return operator;
    }

    private static Job buildJob(
            JobRepository jobRepository,
            DataSourceTransactionManager transactionManager,
            DataSource dataSource,
            RunConfig config,
            String sourceDigest)
            throws Exception {
        ItemReader<SourceRow> reader = config.readerMode() == ReaderMode.CURSOR
                ? cursorReader(dataSource, config.readBatchSize())
                : pagingReader(dataSource, config.readBatchSize());
        ItemProcessor<SourceRow, ProjectedRow> processor = source -> project(config.importName(), sourceDigest, source);
        ItemWriter<ProjectedRow> writer =
                new ParityWriter(dataSource, config.failAfterChunk(), config.pauseConfig());

        Step step = new StepBuilder(STEP_NAME, jobRepository)
                .<SourceRow, ProjectedRow>chunk(config.chunkSize())
                .reader(reader)
                .processor(processor)
                .writer(writer)
                .transactionManager(transactionManager)
                .build();
        return new JobBuilder(JOB_NAME, jobRepository).start(step).build();
    }

    private static ItemReader<SourceRow> cursorReader(DataSource dataSource, int fetchSize) {
        return new JdbcCursorItemReaderBuilder<SourceRow>()
                .name("springCustomerCursorReader")
                .dataSource(dataSource)
                .sql("SELECT customer_id, full_name, is_active, balance_cents "
                        + "FROM app_source.source_customer ORDER BY customer_id")
                .rowMapper((row, rowNum) -> sourceRow(row))
                .fetchSize(fetchSize)
                .saveState(true)
                .connectionAutoCommit(false)
                .build();
    }

    private static ItemReader<SourceRow> pagingReader(DataSource dataSource, int pageSize) throws Exception {
        PostgresPagingQueryProvider provider = new PostgresPagingQueryProvider();
        provider.setSelectClause("customer_id, full_name, is_active, balance_cents");
        provider.setFromClause("FROM app_source.source_customer");
        provider.setSortKeys(Map.of("customer_id", Order.ASCENDING));

        return new JdbcPagingItemReaderBuilder<SourceRow>()
                .name("springCustomerPagingReader")
                .dataSource(dataSource)
                .queryProvider(provider)
                .rowMapper((row, rowNum) -> sourceRow(row))
                .pageSize(pageSize)
                .fetchSize(pageSize)
                .saveState(true)
                .build();
    }

    private static JobParameters jobParameters(RunConfig config, String sourceDigest) {
        return new JobParametersBuilder()
                .addString("import_name", config.importName())
                .addString("source_digest", sourceDigest)
                .addString("reader_mode", config.readerMode().value)
                .addString("definition_revision", config.definitionRevision())
                .addLong("chunk_size", (long) config.chunkSize())
                .addLong("read_batch_size", (long) config.readBatchSize())
                .toJobParameters();
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

    private static ProjectedRow project(String importName, String sourceDigest, SourceRow source)
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

    private static final class ParityWriter implements ItemWriter<ProjectedRow> {
        private final JdbcTemplate jdbcTemplate;
        private final Long failAfterChunk;
        private final PauseConfig pauseConfig;
        private long writeInvocation;

        ParityWriter(DataSource dataSource, Long failAfterChunk, PauseConfig pauseConfig) {
            this.jdbcTemplate = new JdbcTemplate(dataSource);
            this.failAfterChunk = failAfterChunk;
            this.pauseConfig = pauseConfig;
        }

        @Override
        public void write(Chunk<? extends ProjectedRow> chunk) throws Exception {
            List<? extends ProjectedRow> items = chunk.getItems();
            if (items.isEmpty()) {
                return;
            }
            for (int start = 0; start < items.size(); start += ROWS_PER_STATEMENT) {
                int end = Math.min(start + ROWS_PER_STATEMENT, items.size());
                insertMultiValues(items.subList(start, end));
            }
            writeInvocation = Math.addExact(writeInvocation, 1L);
            if (failAfterChunk != null && writeInvocation == failAfterChunk) {
                throw new InjectedFailure(
                        "injected Spring writer failure after business writes before chunk commit at invocation "
                                + writeInvocation);
            }
            if (pauseConfig != null && writeInvocation == pauseConfig.chunk()) {
                long currentChunk = writeInvocation;
                if (pauseConfig.phase() == PausePhase.BEFORE_COMMIT) {
                    pauseAtMarker(pauseConfig, currentChunk);
                } else {
                    if (!TransactionSynchronizationManager.isSynchronizationActive()) {
                        throw new IllegalStateException(
                                "Spring transaction synchronization is not active for after-commit pause");
                    }
                    TransactionSynchronizationManager.registerSynchronization(new TransactionSynchronization() {
                        @Override
                        public void afterCommit() {
                            try {
                                pauseAtMarker(pauseConfig, currentChunk);
                            } catch (Exception error) {
                                throw new IllegalStateException("failed to enter after-commit pause", error);
                            }
                        }
                    });
                }
            }
        }

        private void insertMultiValues(List<? extends ProjectedRow> rows) {
            int boundParameters = Math.multiplyExact(rows.size(), COLUMNS_PER_ROW);
            if (rows.isEmpty() || rows.size() > ROWS_PER_STATEMENT || boundParameters > MAX_BOUND_PARAMETERS) {
                throw new IllegalArgumentException(
                        "writer batch exceeds parity bound of " + ROWS_PER_STATEMENT + " rows / "
                                + MAX_BOUND_PARAMETERS + " parameters");
            }

            StringBuilder sql = new StringBuilder(
                    "INSERT INTO app_business.customer_projection "
                            + "(import_name, source_digest, customer_id, display_name, loyalty_score, is_premium, row_fingerprint) VALUES ");
            for (int row = 0; row < rows.size(); row++) {
                if (row != 0) {
                    sql.append(',');
                }
                sql.append("(?,?,?,?,?,?,?)");
            }

            int affected = jdbcTemplate.update(connection -> {
                PreparedStatement statement = connection.prepareStatement(sql.toString());
                int parameter = 1;
                for (ProjectedRow row : rows) {
                    statement.setString(parameter++, row.importName());
                    statement.setString(parameter++, row.sourceDigest());
                    statement.setLong(parameter++, row.customerId());
                    statement.setString(parameter++, row.displayName());
                    statement.setLong(parameter++, row.loyaltyScore());
                    statement.setBoolean(parameter++, row.premium());
                    statement.setBytes(parameter++, row.fingerprint());
                }
                return statement;
            });
            if (affected != rows.size()) {
                throw new IllegalStateException(
                        "writer affected " + affected + " rows for expected batch size " + rows.size());
            }
        }
    }

    private static void pauseAtMarker(PauseConfig pauseConfig, long chunk) throws Exception {
        long pid = ProcessHandle.current().pid();
        String marker = "pid=" + pid
                + " phase=" + pauseConfig.phase().value
                + " chunk=" + chunk
                + System.lineSeparator();
        Files.writeString(
                pauseConfig.marker(),
                marker,
                StandardCharsets.UTF_8,
                StandardOpenOption.CREATE_NEW,
                StandardOpenOption.WRITE);
        new CountDownLatch(1).await();
    }

    private static DataSource springDataSource(DatabaseConfig database) {
        DriverManagerDataSource dataSource = new DriverManagerDataSource();
        dataSource.setUrl(withSpringSchema(database.url()));
        dataSource.setUsername(database.user());
        dataSource.setPassword(database.password());
        return dataSource;
    }

    private static String withSpringSchema(String url) {
        if (url.toLowerCase(Locale.ROOT).contains("rewritebatchedinserts=true")) {
            throw new IllegalArgumentException("reWriteBatchedInserts=true is forbidden for the parity candidate");
        }
        String separator = url.contains("?") ? "&" : "?";
        return url + separator + "currentSchema=spring_batch,public&reWriteBatchedInserts=false";
    }

    private static Connection connectBase(DatabaseConfig database) throws SQLException {
        if (database.url().toLowerCase(Locale.ROOT).contains("rewritebatchedinserts=true")) {
            throw new IllegalArgumentException("reWriteBatchedInserts=true is forbidden for the parity candidate");
        }
        Properties properties = new Properties();
        properties.setProperty("user", database.user());
        properties.setProperty("password", database.password());
        properties.setProperty("ApplicationName", "oxide-batch-workloads-spring-batch");
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

    private enum PausePhase {
        BEFORE_COMMIT("before-commit"),
        AFTER_COMMIT("after-commit");

        private final String value;

        PausePhase(String value) {
            this.value = value;
        }

        static PausePhase parse(String value) {
            return switch (value) {
                case "before-commit" -> BEFORE_COMMIT;
                case "after-commit" -> AFTER_COMMIT;
                default -> throw new IllegalArgumentException(
                        "--pause-phase must be before-commit or after-commit");
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

    private record PauseConfig(long chunk, PausePhase phase, Path marker) {}

    private record RunConfig(
            String importName,
            ReaderMode readerMode,
            int chunkSize,
            int readBatchSize,
            Long failAfterChunk,
            PauseConfig pauseConfig) {
        String definitionRevision() {
            return readerMode == ReaderMode.CURSOR ? CURSOR_DEFINITION_REVISION : PAGING_DEFINITION_REVISION;
        }

        static RunConfig parse(Options options) {
            return parseCommon(options, true);
        }

        static RunConfig parseForRecover(Options options) {
            RunConfig config = parseCommon(options, false);
            if (config.failAfterChunk() != null || config.pauseConfig() != null) {
                throw new IllegalArgumentException(
                        "recover does not accept --fail-after-chunk or crash pause options");
            }
            return config;
        }

        private static RunConfig parseCommon(Options options, boolean allowFailureControls) {
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

            Long failAfterChunk = optionalPositiveLong(options, "fail-after-chunk");
            boolean hasPauseChunk = options.values().containsKey("pause-at-chunk");
            boolean hasPausePhase = options.values().containsKey("pause-phase");
            boolean hasPauseMarker = options.values().containsKey("pause-marker");
            int pauseParts = (hasPauseChunk ? 1 : 0) + (hasPausePhase ? 1 : 0) + (hasPauseMarker ? 1 : 0);
            if (pauseParts != 0 && pauseParts != 3) {
                throw new IllegalArgumentException(
                        "--pause-at-chunk, --pause-phase, and --pause-marker must be supplied together");
            }
            PauseConfig pause = null;
            if (pauseParts == 3) {
                if (!allowFailureControls) {
                    throw new IllegalArgumentException("recover does not accept crash pause options");
                }
                long pauseChunk = optionalPositiveLong(options, "pause-at-chunk");
                PausePhase pausePhase = PausePhase.parse(required(options, "pause-phase"));
                Path marker = Path.of(required(options, "pause-marker"));
                pause = new PauseConfig(pauseChunk, pausePhase, marker);
            }
            if (!allowFailureControls && failAfterChunk != null) {
                throw new IllegalArgumentException("recover does not accept --fail-after-chunk");
            }
            return new RunConfig(importName, reader, chunkSize, readBatchSize, failAfterChunk, pause);
        }
    }

    private record Options(Map<String, String> values) {
        private static final java.util.Set<String> ALLOWED = java.util.Set.of(
                "import-name",
                "reader",
                "chunk-size",
                "fetch-size",
                "page-size",
                "fail-after-chunk",
                "pause-at-chunk",
                "pause-phase",
                "pause-marker");

        static Options parse(String[] args, int start) {
            Map<String, String> values = new LinkedHashMap<>();
            for (int index = start; index < args.length; index += 2) {
                String key = args[index];
                if (!key.startsWith("--") || index + 1 >= args.length) {
                    throw new IllegalArgumentException("options must be supplied as --key value pairs");
                }
                String normalized = key.substring(2);
                if (!ALLOWED.contains(normalized)) {
                    throw new IllegalArgumentException("unknown option --" + normalized);
                }
                if (values.putIfAbsent(normalized, args[index + 1]) != null) {
                    throw new IllegalArgumentException("duplicate option --" + normalized);
                }
            }
            return new Options(Map.copyOf(values));
        }
    }

    private record Cli(String command, Options options, DatabaseConfig database) {
        static Cli parse(String[] args) {
            if (args.length < 1) {
                throw new IllegalArgumentException("usage: spring-batch <migrate|run|recover> [--key value ...]");
            }
            String command = args[0];
            Options options = Options.parse(args, 1);
            DatabaseConfig database = new DatabaseConfig(
                    env("SPRING_BATCH_DATABASE_URL", "jdbc:postgresql://localhost:5434/postgres_postgres_workload"),
                    env("SPRING_BATCH_DATABASE_USER", "oxide_batch_workload"),
                    env("SPRING_BATCH_DATABASE_PASSWORD", "oxide_batch_workload"));
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
