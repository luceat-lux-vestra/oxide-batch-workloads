package io.oxidebatch.workloads.postgres.benchmark.ci;

import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.time.Instant;
import java.util.Arrays;

/** JDK-only launcher that records Java candidate active-work time without changing either candidate. */
public final class BenchmarkLauncher {
    private BenchmarkLauncher() {}

    public static void main(String[] args) throws Exception {
        if (args.length < 1) {
            throw new IllegalArgumentException("usage: BenchmarkLauncher <main-class> [args ...]");
        }
        Class<?> target = Class.forName(args[0]);
        Method main = target.getMethod("main", String[].class);
        String[] targetArgs = Arrays.copyOfRange(args, 1, args.length);
        long pid = ProcessHandle.current().pid();
        System.err.println("OXIDEBATCH_BENCH_PID=" + pid);

        String activeStartFile = System.getenv("OXIDEBATCH_BENCH_ACTIVE_START_FILE");
        if (activeStartFile != null && !activeStartFile.isBlank()) {
            Instant now = Instant.now();
            long epochNanos = Math.addExact(Math.multiplyExact(now.getEpochSecond(), 1_000_000_000L), now.getNano());
            Files.writeString(
                    Path.of(activeStartFile),
                    "pid=" + pid + " active_start_epoch_ns=" + epochNanos + System.lineSeparator(),
                    StandardCharsets.UTF_8,
                    StandardOpenOption.CREATE_NEW,
                    StandardOpenOption.WRITE);
        }

        long started = System.nanoTime();
        try {
            main.invoke(null, (Object) targetArgs);
        } catch (InvocationTargetException failure) {
            Throwable cause = failure.getCause();
            if (cause instanceof Exception exception) {
                throw exception;
            }
            if (cause instanceof Error error) {
                throw error;
            }
            throw new RuntimeException(cause);
        } finally {
            long elapsed = System.nanoTime() - started;
            System.err.println("OXIDEBATCH_BENCH_ACTIVE_WORK_NS=" + elapsed);
        }
    }
}
