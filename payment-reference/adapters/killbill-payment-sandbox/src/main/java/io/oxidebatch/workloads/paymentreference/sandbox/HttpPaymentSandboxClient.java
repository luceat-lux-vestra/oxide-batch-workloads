package io.oxidebatch.workloads.paymentreference.sandbox;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.IOException;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Objects;

public final class HttpPaymentSandboxClient implements PaymentSandboxClient {
    private static final Duration DEFAULT_REQUEST_TIMEOUT = Duration.ofSeconds(5);

    private final URI baseUri;
    private final HttpClient httpClient;
    private final ObjectMapper objectMapper;
    private final Duration requestTimeout;

    public HttpPaymentSandboxClient(URI baseUri) {
        this(baseUri,
             HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build(),
             new ObjectMapper(),
             DEFAULT_REQUEST_TIMEOUT);
    }

    HttpPaymentSandboxClient(URI baseUri, HttpClient httpClient, ObjectMapper objectMapper, Duration requestTimeout) {
        this.baseUri = validateBaseUri(baseUri);
        this.httpClient = Objects.requireNonNull(httpClient, "httpClient");
        this.objectMapper = Objects.requireNonNull(objectMapper, "objectMapper");
        this.requestTimeout = Objects.requireNonNull(requestTimeout, "requestTimeout");
    }

    @Override
    public PaymentSandboxPayment createPayment(String providerPaymentId, long amountMinor, String currency,
                                               String idempotencyKey, String correlationId) {
        Map<String, Object> body = amountBody(amountMinor, currency);
        body.put("id", providerPaymentId);
        return mutate("/payments", body, providerPaymentId, idempotencyKey, correlationId);
    }

    @Override
    public PaymentSandboxPayment authorize(String providerPaymentId, String idempotencyKey, String correlationId) {
        return mutate(paymentPath(providerPaymentId) + "/authorize", null, providerPaymentId, idempotencyKey, correlationId);
    }

    @Override
    public PaymentSandboxPayment capture(String providerPaymentId, long amountMinor, String currency,
                                         String idempotencyKey, String correlationId) {
        return mutate(paymentPath(providerPaymentId) + "/capture", amountBody(amountMinor, currency),
                      providerPaymentId, idempotencyKey, correlationId);
    }

    @Override
    public PaymentSandboxPayment cancel(String providerPaymentId, String idempotencyKey, String correlationId) {
        return mutate(paymentPath(providerPaymentId) + "/cancel", null, providerPaymentId, idempotencyKey, correlationId);
    }

    @Override
    public PaymentSandboxPayment refund(String providerPaymentId, long amountMinor, String currency,
                                        String idempotencyKey, String correlationId) {
        return mutate(paymentPath(providerPaymentId) + "/refund", amountBody(amountMinor, currency),
                      providerPaymentId, idempotencyKey, correlationId);
    }

    @Override
    public PaymentSandboxPayment getPayment(String providerPaymentId, String correlationId) {
        requireNonBlank(providerPaymentId, "providerPaymentId");
        requireNonBlank(correlationId, "correlationId");
        HttpRequest request = HttpRequest.newBuilder(resolve(paymentPath(providerPaymentId)))
                .timeout(requestTimeout)
                .header("Accept", "application/json")
                .header("X-Correlation-ID", correlationId)
                .GET()
                .build();
        return send(request, false, providerPaymentId, null);
    }

    private PaymentSandboxPayment mutate(String path, Map<String, Object> body, String providerPaymentId,
                                         String idempotencyKey, String correlationId) {
        requireNonBlank(providerPaymentId, "providerPaymentId");
        requireNonBlank(idempotencyKey, "idempotencyKey");
        requireNonBlank(correlationId, "correlationId");

        HttpRequest.Builder builder = HttpRequest.newBuilder(resolve(path))
                .timeout(requestTimeout)
                .header("Accept", "application/json")
                .header("Idempotency-Key", idempotencyKey)
                .header("X-Correlation-ID", correlationId);
        String json = "";
        if (body != null) {
            try {
                json = objectMapper.writeValueAsString(body);
            } catch (IOException e) {
                throw new IllegalStateException("Unable to serialize payment-sandbox request", e);
            }
            builder.header("Content-Type", "application/json");
        }
        return send(builder.POST(HttpRequest.BodyPublishers.ofString(json)).build(), true, providerPaymentId, idempotencyKey);
    }

    private PaymentSandboxPayment send(HttpRequest request, boolean mutation, String providerPaymentId, String idempotencyKey) {
        final HttpResponse<String> response;
        try {
            response = httpClient.send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
        } catch (IOException e) {
            throw PaymentSandboxException.transport("Transport failure calling payment-sandbox", mutation,
                                                    providerPaymentId, idempotencyKey, e);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw PaymentSandboxException.transport("Interrupted while calling payment-sandbox", mutation,
                                                    providerPaymentId, idempotencyKey, e);
        }

        if (response.statusCode() >= 200 && response.statusCode() < 300) {
            try {
                return objectMapper.readValue(response.body(), PaymentSandboxPayment.class);
            } catch (IOException e) {
                throw new PaymentSandboxException("Invalid payment-sandbox success response", response.statusCode(),
                        "invalid_response", false, false, providerPaymentId, idempotencyKey, e);
            }
        }

        String errorCode = "http_" + response.statusCode();
        String message = "payment-sandbox returned HTTP " + response.statusCode();
        try {
            JsonNode error = objectMapper.readTree(response.body()).path("error");
            if (error.hasNonNull("code")) errorCode = error.get("code").asText();
            if (error.hasNonNull("message")) message = error.get("message").asText();
        } catch (IOException ignored) {
            // HTTP classification remains authoritative if the error body is malformed.
        }
        throw PaymentSandboxException.http(message, response.statusCode(), errorCode, providerPaymentId, idempotencyKey);
    }

    private URI resolve(String path) {
        return baseUri.resolve(path.startsWith("/") ? path.substring(1) : path);
    }

    private static String paymentPath(String providerPaymentId) {
        return "/payments/" + URLEncoder.encode(providerPaymentId, StandardCharsets.UTF_8).replace("+", "%20");
    }

    private static Map<String, Object> amountBody(long amountMinor, String currency) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("amount", amountMinor);
        body.put("currency", currency);
        return body;
    }

    private static URI validateBaseUri(URI baseUri) {
        Objects.requireNonNull(baseUri, "baseUri");
        String scheme = baseUri.getScheme();
        if (!("http".equalsIgnoreCase(scheme) || "https".equalsIgnoreCase(scheme))) {
            throw new IllegalArgumentException("payment-sandbox base URI must use http or https");
        }
        if (baseUri.getHost() == null || baseUri.getHost().isEmpty()) {
            throw new IllegalArgumentException("payment-sandbox base URI must include a host");
        }
        if (baseUri.getQuery() != null || baseUri.getFragment() != null) {
            throw new IllegalArgumentException("payment-sandbox base URI must not contain query or fragment");
        }
        String normalized = baseUri.toString();
        return URI.create(normalized.endsWith("/") ? normalized : normalized + "/");
    }

    private static void requireNonBlank(String value, String name) {
        if (value == null || value.trim().isEmpty()) {
            throw new IllegalArgumentException(name + " must not be blank");
        }
    }
}
