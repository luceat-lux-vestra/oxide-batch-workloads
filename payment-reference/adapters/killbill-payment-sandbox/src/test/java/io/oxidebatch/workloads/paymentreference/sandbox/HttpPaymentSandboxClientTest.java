package io.oxidebatch.workloads.paymentreference.sandbox;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicReference;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class HttpPaymentSandboxClientTest {
    private HttpServer server;

    @AfterEach
    void stopServer() {
        if (server != null) {
            server.stop(0);
        }
    }

    @Test
    void createPaymentUsesPinnedContractHeadersAndBody() throws Exception {
        AtomicReference<String> requestMethod = new AtomicReference<>();
        AtomicReference<String> requestBody = new AtomicReference<>();
        AtomicReference<String> idempotency = new AtomicReference<>();
        AtomicReference<String> correlation = new AtomicReference<>();

        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/payments", exchange -> {
            requestMethod.set(exchange.getRequestMethod());
            requestBody.set(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            idempotency.set(exchange.getRequestHeaders().getFirst("Idempotency-Key"));
            correlation.set(exchange.getRequestHeaders().getFirst("X-Correlation-ID"));
            respond(exchange, 201, paymentJson("kb-test", 1000, "USD", "pending", 0, 0, 0, 1));
        });
        server.start();

        HttpPaymentSandboxClient client = new HttpPaymentSandboxClient(baseUri());
        PaymentSandboxPayment payment = client.createPayment("kb-test", 1000L, "USD", "idem-1", "corr-1");

        assertEquals("POST", requestMethod.get());
        assertEquals("idem-1", idempotency.get());
        assertEquals("corr-1", correlation.get());
        assertTrue(requestBody.get().contains("\"id\":\"kb-test\""));
        assertTrue(requestBody.get().contains("\"amount\":1000"));
        assertTrue(requestBody.get().contains("\"currency\":\"USD\""));
        assertEquals("kb-test", payment.getId());
        assertEquals("pending", payment.getStatus());
    }

    @Test
    void mutationPathsMatchPaymentSandboxV154Contract() throws Exception {
        AtomicReference<String> seenPath = new AtomicReference<>();
        AtomicReference<String> seenBody = new AtomicReference<>();
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/payments/kb-1/authorize", exchange -> {
            seenPath.set(exchange.getRequestURI().getPath());
            seenBody.set(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            respond(exchange, 200, paymentJson("kb-1", 100, "USD", "authorized", 100, 0, 0, 2));
        });
        server.createContext("/payments/kb-1/capture", exchange -> {
            seenPath.set(exchange.getRequestURI().getPath());
            seenBody.set(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            respond(exchange, 200, paymentJson("kb-1", 100, "USD", "captured", 100, 100, 0, 3));
        });
        server.start();

        HttpPaymentSandboxClient client = new HttpPaymentSandboxClient(baseUri());
        client.authorize("kb-1", "idem-a", "corr");
        assertEquals("/payments/kb-1/authorize", seenPath.get());
        assertEquals("", seenBody.get());

        client.capture("kb-1", 100, "USD", "idem-c", "corr");
        assertEquals("/payments/kb-1/capture", seenPath.get());
        assertTrue(seenBody.get().contains("\"amount\":100"));
        assertTrue(seenBody.get().contains("\"currency\":\"USD\""));
    }

    @Test
    void provider503IsRetryableButNotAmbiguousBecauseResponseWasObserved() throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/payments/kb-1/authorize", exchange ->
                respond(exchange, 503, "{\"error\":{\"code\":\"provider_unavailable\",\"message\":\"try later\"}}"));
        server.start();

        HttpPaymentSandboxClient client = new HttpPaymentSandboxClient(baseUri());
        PaymentSandboxException error = assertThrows(PaymentSandboxException.class,
                () -> client.authorize("kb-1", "idem-a", "corr"));

        assertEquals(503, error.getHttpStatus());
        assertEquals("provider_unavailable", error.getErrorCode());
        assertTrue(error.isRetryable());
        assertFalse(error.isAmbiguous());
    }

    @Test
    void transportFailureOnMutationIsAmbiguousAndRetainsIdempotencyIdentity() throws Exception {
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        int port = server.getAddress().getPort();
        server.start();
        server.stop(0);
        server = null;

        HttpPaymentSandboxClient client = new HttpPaymentSandboxClient(URI.create("http://127.0.0.1:" + port + "/"));
        PaymentSandboxException error = assertThrows(PaymentSandboxException.class,
                () -> client.authorize("kb-ambiguous", "idem-ambiguous", "corr"));

        assertEquals(0, error.getHttpStatus());
        assertTrue(error.isRetryable());
        assertTrue(error.isAmbiguous());
        assertEquals("idem-ambiguous", error.getIdempotencyKey());
        assertEquals("kb-ambiguous", error.getProviderPaymentId());
    }

    @Test
    void thereIsNoImplicitCloudEndpoint() {
        assertThrows(NullPointerException.class, () -> new HttpPaymentSandboxClient(null));
        assertThrows(IllegalArgumentException.class,
                () -> new HttpPaymentSandboxClient(URI.create("ftp://payment-sandbox/")));
    }

    private URI baseUri() {
        return URI.create("http://127.0.0.1:" + server.getAddress().getPort() + "/");
    }

    private static void respond(HttpExchange exchange, int status, String body) throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json");
        exchange.sendResponseHeaders(status, bytes.length);
        exchange.getResponseBody().write(bytes);
        exchange.close();
    }

    private static String paymentJson(String id,
                                      long amount,
                                      String currency,
                                      String status,
                                      long authorized,
                                      long captured,
                                      long refunded,
                                      long version) {
        return "{"
                + "\"id\":\"" + id + "\","
                + "\"amount\":" + amount + ","
                + "\"currency\":\"" + currency + "\","
                + "\"status\":\"" + status + "\","
                + "\"authorized_amount\":" + authorized + ","
                + "\"captured_amount\":" + captured + ","
                + "\"refunded_amount\":" + refunded + ","
                + "\"version\":" + version
                + "}";
    }
}
