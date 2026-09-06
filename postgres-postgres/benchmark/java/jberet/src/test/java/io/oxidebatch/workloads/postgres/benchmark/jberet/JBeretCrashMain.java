package io.oxidebatch.workloads.postgres.benchmark.jberet;

import java.io.Serializable;
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
import java.time.Duration;
import java.util.HexFormat;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Properties;
import java.util.Set;
import java.util.concurrent.CountDownLatch;

import jakarta.batch.api.BatchProperty;
import jakarta.batch.api.chunk.listener.ItemWriteListener;
import jakarta.batch.operations.JobOperator;
import jakarta.batch.runtime.BatchRuntime;
import jakarta.batch.runtime.BatchStatus;
import jakarta.batch.runtime.JobExecution;
import jakarta.batch.runtime.Metric;
import jakarta.batch.runtime.StepExecution;
import jakarta.inject.Inject;

/** Test-only external-crash driver for campaign #86 PR2. */
public final class JBeretCrashMain {
    private static final String JOB_XML_NAME = "postgres-postgres-crash";
    private static final int DEFAULT_MAX_BIND_PARAMS = 2_000;
    private static final int DEFAULT_FETCH_SIZE = 500;
    private static final int DEFAULT_PAGE_SIZE = 750;
    private static final int MAX_SIZE = 1_000_000;
    private static final Duration JOB_TIMEOUT = Duration.ofMinutes(5);

    private JBeretCrashMain() {}

    public static void main(String[] args) throws Exception {
        Cli cli = Cli.parse(args);
        DatabaseConfig database = DatabaseConfig.fromEnvironment();
        switch (cli.command()) {
            case "run" -> run(database, RunConfig.parse(cli.options()));
            case "inspect" -> inspect(Long.parseLong(required(cli.options(), "execution-id")));
            case "restart" -> restart(database, Long.parseLong(required(cli.options(), "execution-id")));
            default -> throw new IllegalArgumentException("unknown command: " + cli.command());
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
            parameters.setProperty("readerMode", config.readerMode());
            parameters.setProperty("chunkSize", Integer.toString(config.chunkSize()));
            parameters.setProperty("fetchSize", Integer.toString(config.fetchSize()));
            parameters.setProperty("pageSize", Integer.toString(config.pageSize()));
            parameters.setProperty("maxBindParams", Integer.toString(DEFAULT_MAX_BIND_PARAMS));
            parameters.setProperty("definitionRevision", "jberet-postgres-postgres-" + config.readerMode() + "-v1");
            parameters.setProperty("pauseAtChunk", Integer.toString(config.pauseAtChunk()));
            parameters.setProperty("pausePhase", config.pausePhase());
            parameters.setProperty("pauseMarker", config.pauseMarker());

            JobOperator operator = BatchRuntime.getJobOperator();
            long executionId = operator.start(JOB_XML_NAME, parameters);
            System.out.printf(Locale.ROOT, "started_execution_id=%d source_digest=%s%n", executionId, sourceDigest);
            System.out.flush();
            JobExecution execution = awaitTerminal(operator, executionId);
            System.out.printf(Locale.ROOT, "unexpected_terminal_execution_id=%d status=%s%n",
                    executionId, execution.getBatchStatus());
            if (execution.getBatchStatus() != BatchStatus.COMPLETED) {
                throw new IllegalStateException("instrumented JBeret execution terminated before external SIGKILL: "
                        + execution.getBatchStatus());
            }
        }
    }

    private static void inspect(long executionId) {
        if (executionId < 1L) {
            throw new IllegalArgumentException("execution-id must be positive");
        }
        JobOperator operator = BatchRuntime.getJobOperator();
        JobExecution execution = operator.getJobExecution(executionId);
        long readCount = 0L;
        long writeCount = 0L;
        long commitCount = 0L;
        List<StepExecution> steps = operator.getStepExecutions(executionId);
        for (StepExecution step : steps) {
            for (Metric metric : step.getMetrics()) {
                switch (metric.getType()) {
                    case READ_COUNT -> readCount = Math.addExact(readCount, metric.getValue());
                    case WRITE_COUNT -> writeCount = Math.addExact(writeCount, metric.getValue());
                    case COMMIT_COUNT -> commitCount = Math.addExact(commitCount, metric.getValue());
                    default -> { }
                }
            }
        }
        System.out.printf(Locale.ROOT,
                "execution_id=%d status=%s step_count=%d read_count=%d write_count=%d commit_count=%d%n",
                executionId,
                execution.getBatchStatus(),
                steps.size(),
                readCount,
                writeCount,
                commitCount);
    }

    private static void restart(DatabaseConfig database, long executionId) throws Exception {
        if (executionId < 1L) {
            throw new IllegalArgumentException("execution-id must be positive");
        }
        try (Connection sourceLock = connect(database)) {
            sourceLock.setAutoCommit(false);
            try (Statement lock = sourceLock.createStatement()) {
                lock.execute("LOCK TABLE app_source.source_customer IN SHARE MODE");
            }

            JobOperator operator = BatchRuntime.getJobOperator();
            Properties original = operator.getParameters(executionId);
            String expectedDigest = requiredProperty(original, "sourceDigest", executionId);
            String currentDigest = sourceDigest(sourceLock);
            if (!expectedDigest.equals(currentDigest)) {
                throw new IllegalStateException("source digest changed for existing JBeret execution: execution_id="
                        + executionId + " prior=" + expectedDigest + " current=" + currentDigest);
            }

            Properties restartParameters = new Properties();
            restartParameters.setProperty("pauseAtChunk", "0");
            restartParameters.setProperty("pausePhase", "none");
            restartParameters.setProperty("pauseMarker", "");
            long restartedId = operator.restart(executionId, restartParameters);
            System.out.printf(Locale.ROOT, "restarted_execution_id=%d old_execution_id=%d%n", restartedId, executionId);
            System.out.flush();
            JobExecution restarted = awaitTerminal(operator, restartedId);
            System.out.printf(Locale.ROOT, "restart_terminal_execution_id=%d status=%s exit_status=%s%n",
                    restartedId, restarted.getBatchStatus(), restarted.getExitStatus());
            if (restarted.getBatchStatus() != BatchStatus.COMPLETED) {
                throw new IllegalStateException("JBeret restarted execution did not complete: " + restarted.getBatchStatus());
            }
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
                throw new IllegalStateException("timed out waiting for JBeret execution " + executionId + " in status " + status);
            }
            Thread.sleep(25L);
        }
    }

    private static String requiredProperty(Properties properties, String key, long executionId) {
        String value = properties.getProperty(key);
        if (value == null || value.isBlank()) {
            throw new IllegalStateException("JBeret execution " + executionId + " is missing persisted parameter " + key);
        }
        return value;
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

    private static Connection connect(DatabaseConfig database) throws SQLException {
        Properties properties = new Properties();
        properties.setProperty("user", database.user());
        properties.setProperty("password", database.password());
        properties.setProperty("ApplicationName", "oxide-batch-workloads-jberet-crash");
        properties.setProperty("reWriteBatchedInserts", "false");
        return DriverManager.getConnection(database.url(), properties);
    }

    public static final class CrashBoundaryListener implements ItemWriteListener {
        @Inject
        @BatchProperty(name = "pauseAtChunk")
        private String pauseAtChunk;

        @Inject
        @BatchProperty(name = "pausePhase")
        private String pausePhase;

        @Inject
        @BatchProperty(name = "pauseMarker")
        private String pauseMarker;

        private int writeAttempts;

        public CrashBoundaryListener() { }

        @Override
        public void beforeWrite(List<Object> items) throws Exception {
            writeAttempts = Math.addExact(writeAttempts, 1);
            pause("before-write");
        }

        @Override
        public void afterWrite(List<Object> items) throws Exception {
            pause("after-write");
        }

        @Override
        public void onWriteError(List<Object> items, Exception ex) { }

        private void pause(String boundary) throws Exception {
            int target = Integer.parseInt(pauseAtChunk);
            if (target == 0 || writeAttempts != target || !boundary.equals(pausePhase)) {
                return;
            }
            Path marker = Path.of(pauseMarker);
            if (!marker.isAbsolute()) {
                throw new IllegalArgumentException("pauseMarker must be absolute");
            }
            Files.writeString(
                    marker,
                    "pid=" + ProcessHandle.current().pid() + " phase=" + boundary + " chunk=" + writeAttempts + "\n",
                    StandardCharsets.UTF_8,
                    StandardOpenOption.CREATE_NEW,
                    StandardOpenOption.WRITE);
            new CountDownLatch(1).await();
        }
    }

    private record DatabaseConfig(String url, String user, String password) {
        static DatabaseConfig fromEnvironment() {
            return new DatabaseConfig(requiredEnv("JBERET_DATABASE_URL"),
                    requiredEnv("JBERET_DATABASE_USER"), requiredEnv("JBERET_DATABASE_PASSWORD"));
        }
    }

    private record RunConfig(String importName, String readerMode, int chunkSize, int fetchSize, int pageSize,
                             int pauseAtChunk, String pausePhase, String pauseMarker) {
        static RunConfig parse(Map<String, String> options) {
            String reader = required(options, "reader");
            if (!Set.of("cursor", "paging").contains(reader)) {
                throw new IllegalArgumentException("reader must be cursor or paging");
            }
            int chunk = positiveInt(required(options, "chunk-size"), "chunk-size");
            int fetch = positiveInt(options.getOrDefault("fetch-size", Integer.toString(DEFAULT_FETCH_SIZE)), "fetch-size");
            int page = positiveInt(options.getOrDefault("page-size", Integer.toString(DEFAULT_PAGE_SIZE)), "page-size");
            int pauseChunk = positiveInt(required(options, "pause-at-chunk"), "pause-at-chunk");
            String phase = required(options, "pause-phase");
            if (!Set.of("before-write", "after-write").contains(phase)) {
                throw new IllegalArgumentException("pause-phase must be before-write or after-write");
            }
            Path marker = Path.of(required(options, "pause-marker"));
            if (!marker.isAbsolute()) {
                throw new IllegalArgumentException("pause-marker must be absolute");
            }
            return new RunConfig(required(options, "import-name"), reader, chunk, fetch, page, pauseChunk, phase, marker.toString());
        }
    }

    private static int positiveInt(String value, String field) {
        int parsed = Integer.parseInt(value);
        if (parsed < 1 || parsed > MAX_SIZE) {
            throw new IllegalArgumentException(field + " must be between 1 and " + MAX_SIZE);
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

    private record Cli(String command, Map<String, String> options) {
        static Cli parse(String[] args) {
            if (args.length == 0) {
                throw new IllegalArgumentException("usage: JBeretCrashMain <run|inspect|restart> [options]");
            }
            java.util.LinkedHashMap<String, String> values = new java.util.LinkedHashMap<>();
            for (int index = 1; index < args.length; index += 2) {
                if (!args[index].startsWith("--") || index + 1 >= args.length) {
                    throw new IllegalArgumentException("options must be --name value pairs");
                }
                String key = args[index].substring(2);
                if (values.putIfAbsent(key, args[index + 1]) != null) {
                    throw new IllegalArgumentException("duplicate option --" + key);
                }
            }
            return new Cli(args[0], Map.copyOf(values));
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
