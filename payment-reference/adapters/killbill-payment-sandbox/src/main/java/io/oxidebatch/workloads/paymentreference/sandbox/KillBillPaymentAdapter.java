package io.oxidebatch.workloads.paymentreference.sandbox;

import java.util.Locale;
import java.util.Objects;
import java.util.UUID;
import java.util.regex.Pattern;

public final class KillBillPaymentAdapter {
    private static final Pattern CURRENCY = Pattern.compile("[A-Z]{3}");

    private final PaymentSandboxClient client;

    public KillBillPaymentAdapter(PaymentSandboxClient client) {
        this.client = Objects.requireNonNull(client, "client");
    }

    public KillBillPaymentResult authorize(UUID killBillPaymentId,
                                           UUID killBillTransactionId,
                                           long amountMinor,
                                           String currency) {
        validateInitialMutation(killBillPaymentId, killBillTransactionId, amountMinor, currency);
        String providerPaymentId = providerPaymentId(killBillPaymentId);
        ensurePayment(providerPaymentId, killBillPaymentId, killBillTransactionId, amountMinor, currency);
        PaymentSandboxPayment payment = client.authorize(
                providerPaymentId,
                operationIdempotencyKey(killBillTransactionId, "authorize"),
                correlationId(killBillTransactionId));
        return result(killBillPaymentId, killBillTransactionId, KillBillOperation.AUTHORIZE, payment);
    }

    public KillBillPaymentResult purchase(UUID killBillPaymentId,
                                          UUID killBillTransactionId,
                                          long amountMinor,
                                          String currency) {
        validateInitialMutation(killBillPaymentId, killBillTransactionId, amountMinor, currency);
        String providerPaymentId = providerPaymentId(killBillPaymentId);
        ensurePayment(providerPaymentId, killBillPaymentId, killBillTransactionId, amountMinor, currency);

        client.authorize(
                providerPaymentId,
                operationIdempotencyKey(killBillTransactionId, "purchase-authorize"),
                correlationId(killBillTransactionId));

        PaymentSandboxPayment captured = client.capture(
                providerPaymentId,
                amountMinor,
                normalizeCurrency(currency),
                operationIdempotencyKey(killBillTransactionId, "purchase-capture"),
                correlationId(killBillTransactionId));

        return result(killBillPaymentId, killBillTransactionId, KillBillOperation.PURCHASE, captured);
    }

    public KillBillPaymentResult capture(UUID killBillPaymentId,
                                         UUID killBillTransactionId,
                                         long amountMinor,
                                         String currency) {
        validateFollowUpMutation(killBillPaymentId, killBillTransactionId, amountMinor, currency);
        PaymentSandboxPayment payment = client.capture(
                providerPaymentId(killBillPaymentId),
                amountMinor,
                normalizeCurrency(currency),
                operationIdempotencyKey(killBillTransactionId, "capture"),
                correlationId(killBillTransactionId));
        return result(killBillPaymentId, killBillTransactionId, KillBillOperation.CAPTURE, payment);
    }

    public KillBillPaymentResult voidPayment(UUID killBillPaymentId, UUID killBillTransactionId) {
        validateIds(killBillPaymentId, killBillTransactionId);
        PaymentSandboxPayment payment = client.cancel(
                providerPaymentId(killBillPaymentId),
                operationIdempotencyKey(killBillTransactionId, "void"),
                correlationId(killBillTransactionId));
        return result(killBillPaymentId, killBillTransactionId, KillBillOperation.VOID, payment);
    }

    public KillBillPaymentResult refund(UUID killBillPaymentId,
                                        UUID killBillTransactionId,
                                        long amountMinor,
                                        String currency) {
        validateFollowUpMutation(killBillPaymentId, killBillTransactionId, amountMinor, currency);
        PaymentSandboxPayment payment = client.refund(
                providerPaymentId(killBillPaymentId),
                amountMinor,
                normalizeCurrency(currency),
                operationIdempotencyKey(killBillTransactionId, "refund"),
                correlationId(killBillTransactionId));
        return result(killBillPaymentId, killBillTransactionId, KillBillOperation.REFUND, payment);
    }

    public KillBillPaymentResult getPayment(UUID killBillPaymentId) {
        Objects.requireNonNull(killBillPaymentId, "killBillPaymentId");
        PaymentSandboxPayment payment = client.getPayment(
                providerPaymentId(killBillPaymentId),
                "kb-payment:" + killBillPaymentId);
        return result(killBillPaymentId, null, KillBillOperation.GET, payment);
    }

    public static String providerPaymentId(UUID killBillPaymentId) {
        Objects.requireNonNull(killBillPaymentId, "killBillPaymentId");
        return "kb-" + killBillPaymentId;
    }

    public static String createIdempotencyKey(UUID killBillPaymentId) {
        Objects.requireNonNull(killBillPaymentId, "killBillPaymentId");
        return "kb-payment:" + killBillPaymentId + ":create";
    }

    public static String operationIdempotencyKey(UUID killBillTransactionId, String operation) {
        Objects.requireNonNull(killBillTransactionId, "killBillTransactionId");
        if (operation == null || operation.trim().isEmpty()) {
            throw new IllegalArgumentException("operation must not be blank");
        }
        return "kb-txn:" + killBillTransactionId + ":" + operation.toLowerCase(Locale.ROOT);
    }

    private void ensurePayment(String providerPaymentId,
                               UUID killBillPaymentId,
                               UUID killBillTransactionId,
                               long amountMinor,
                               String currency) {
        String normalizedCurrency = normalizeCurrency(currency);
        try {
            client.createPayment(
                    providerPaymentId,
                    amountMinor,
                    normalizedCurrency,
                    createIdempotencyKey(killBillPaymentId),
                    correlationId(killBillTransactionId));
        } catch (PaymentSandboxException e) {
            if (!e.isConflict()) {
                throw e;
            }
            PaymentSandboxPayment existing = client.getPayment(providerPaymentId, correlationId(killBillTransactionId));
            if (existing.getAmount() != amountMinor || !normalizedCurrency.equals(existing.getCurrency())) {
                throw new IllegalStateException(
                        "Existing provider payment conflicts with Kill Bill payment identity " + killBillPaymentId);
            }
        }
    }

    private static KillBillPaymentResult result(UUID killBillPaymentId,
                                                UUID killBillTransactionId,
                                                KillBillOperation operation,
                                                PaymentSandboxPayment payment) {
        String expectedProviderPaymentId = providerPaymentId(killBillPaymentId);
        if (!expectedProviderPaymentId.equals(payment.getId())) {
            throw new IllegalStateException(
                    "Provider payment identity mismatch: expected " + expectedProviderPaymentId + " but got " + payment.getId());
        }
        return new KillBillPaymentResult(
                killBillPaymentId,
                killBillTransactionId,
                payment.getId(),
                operation,
                KillBillAdapterStatus.fromProviderStatus(payment.getStatus()),
                payment.getStatus(),
                payment.getAmount(),
                payment.getCurrency(),
                payment.getAuthorizedAmount(),
                payment.getCapturedAmount(),
                payment.getRefundedAmount(),
                payment.getVersion());
    }

    private static void validateInitialMutation(UUID paymentId,
                                                UUID transactionId,
                                                long amountMinor,
                                                String currency) {
        validateFollowUpMutation(paymentId, transactionId, amountMinor, currency);
    }

    private static void validateFollowUpMutation(UUID paymentId,
                                                 UUID transactionId,
                                                 long amountMinor,
                                                 String currency) {
        validateIds(paymentId, transactionId);
        if (amountMinor <= 0) {
            throw new IllegalArgumentException("amountMinor must be positive");
        }
        normalizeCurrency(currency);
    }

    private static void validateIds(UUID paymentId, UUID transactionId) {
        Objects.requireNonNull(paymentId, "killBillPaymentId");
        Objects.requireNonNull(transactionId, "killBillTransactionId");
    }

    private static String normalizeCurrency(String currency) {
        if (currency == null) {
            throw new IllegalArgumentException("currency must not be null");
        }
        String normalized = currency.toUpperCase(Locale.ROOT);
        if (!CURRENCY.matcher(normalized).matches()) {
            throw new IllegalArgumentException("currency must be a three-letter code");
        }
        return normalized;
    }

    private static String correlationId(UUID transactionId) {
        return "kb-txn:" + transactionId;
    }
}
