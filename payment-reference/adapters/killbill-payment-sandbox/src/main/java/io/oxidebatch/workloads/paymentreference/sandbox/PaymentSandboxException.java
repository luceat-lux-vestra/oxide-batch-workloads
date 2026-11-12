package io.oxidebatch.workloads.paymentreference.sandbox;

public final class PaymentSandboxException extends RuntimeException {
    private final int httpStatus;
    private final String errorCode;
    private final boolean retryable;
    private final boolean ambiguous;
    private final String providerPaymentId;
    private final String idempotencyKey;

    public PaymentSandboxException(String message,
                                   int httpStatus,
                                   String errorCode,
                                   boolean retryable,
                                   boolean ambiguous,
                                   String providerPaymentId,
                                   String idempotencyKey,
                                   Throwable cause) {
        super(message, cause);
        this.httpStatus = httpStatus;
        this.errorCode = errorCode;
        this.retryable = retryable;
        this.ambiguous = ambiguous;
        this.providerPaymentId = providerPaymentId;
        this.idempotencyKey = idempotencyKey;
    }

    public static PaymentSandboxException http(String message,
                                               int httpStatus,
                                               String errorCode,
                                               String providerPaymentId,
                                               String idempotencyKey) {
        boolean retryable = httpStatus == 429 || httpStatus >= 500;
        return new PaymentSandboxException(
                message,
                httpStatus,
                errorCode,
                retryable,
                false,
                providerPaymentId,
                idempotencyKey,
                null);
    }

    public static PaymentSandboxException transport(String message,
                                                    boolean mutation,
                                                    String providerPaymentId,
                                                    String idempotencyKey,
                                                    Throwable cause) {
        return new PaymentSandboxException(
                message,
                0,
                "transport_error",
                true,
                mutation,
                providerPaymentId,
                idempotencyKey,
                cause);
    }

    public int getHttpStatus() {
        return httpStatus;
    }

    public String getErrorCode() {
        return errorCode;
    }

    public boolean isRetryable() {
        return retryable;
    }

    public boolean isAmbiguous() {
        return ambiguous;
    }

    public String getProviderPaymentId() {
        return providerPaymentId;
    }

    public String getIdempotencyKey() {
        return idempotencyKey;
    }

    public boolean isConflict() {
        return httpStatus == 409;
    }

    public boolean isNotFound() {
        return httpStatus == 404;
    }
}
