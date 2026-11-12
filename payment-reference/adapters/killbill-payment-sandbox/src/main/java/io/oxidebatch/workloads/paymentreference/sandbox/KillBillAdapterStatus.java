package io.oxidebatch.workloads.paymentreference.sandbox;

public enum KillBillAdapterStatus {
    PROCESSED,
    PENDING,
    CANCELED,
    ERROR;

    public static KillBillAdapterStatus fromProviderStatus(String providerStatus) {
        if (providerStatus == null) {
            throw new IllegalArgumentException("providerStatus must not be null");
        }
        switch (providerStatus) {
            case "pending":
                return PENDING;
            case "authorized":
            case "partially_captured":
            case "captured":
            case "partially_refunded":
            case "refunded":
                return PROCESSED;
            case "cancelled":
                return CANCELED;
            case "failed":
                return ERROR;
            default:
                throw new IllegalArgumentException("Unsupported payment-sandbox status: " + providerStatus);
        }
    }
}
