package io.oxidebatch.workloads.paymentreference.sandbox;

public interface PaymentSandboxClient {
    PaymentSandboxPayment createPayment(String providerPaymentId,
                                        long amountMinor,
                                        String currency,
                                        String idempotencyKey,
                                        String correlationId);

    PaymentSandboxPayment authorize(String providerPaymentId,
                                    String idempotencyKey,
                                    String correlationId);

    PaymentSandboxPayment capture(String providerPaymentId,
                                  long amountMinor,
                                  String currency,
                                  String idempotencyKey,
                                  String correlationId);

    PaymentSandboxPayment cancel(String providerPaymentId,
                                 String idempotencyKey,
                                 String correlationId);

    PaymentSandboxPayment refund(String providerPaymentId,
                                 long amountMinor,
                                 String currency,
                                 String idempotencyKey,
                                 String correlationId);

    PaymentSandboxPayment getPayment(String providerPaymentId, String correlationId);
}
