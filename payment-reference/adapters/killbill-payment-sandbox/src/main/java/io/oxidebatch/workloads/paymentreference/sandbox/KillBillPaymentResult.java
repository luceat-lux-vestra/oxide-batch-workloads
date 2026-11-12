package io.oxidebatch.workloads.paymentreference.sandbox;

import java.util.UUID;

public final class KillBillPaymentResult {
    private final UUID killBillPaymentId;
    private final UUID killBillTransactionId;
    private final String providerPaymentId;
    private final KillBillOperation operation;
    private final KillBillAdapterStatus status;
    private final String providerStatus;
    private final long amountMinor;
    private final String currency;
    private final long authorizedAmountMinor;
    private final long capturedAmountMinor;
    private final long refundedAmountMinor;
    private final long providerVersion;

    public KillBillPaymentResult(UUID killBillPaymentId,
                                 UUID killBillTransactionId,
                                 String providerPaymentId,
                                 KillBillOperation operation,
                                 KillBillAdapterStatus status,
                                 String providerStatus,
                                 long amountMinor,
                                 String currency,
                                 long authorizedAmountMinor,
                                 long capturedAmountMinor,
                                 long refundedAmountMinor,
                                 long providerVersion) {
        this.killBillPaymentId = killBillPaymentId;
        this.killBillTransactionId = killBillTransactionId;
        this.providerPaymentId = providerPaymentId;
        this.operation = operation;
        this.status = status;
        this.providerStatus = providerStatus;
        this.amountMinor = amountMinor;
        this.currency = currency;
        this.authorizedAmountMinor = authorizedAmountMinor;
        this.capturedAmountMinor = capturedAmountMinor;
        this.refundedAmountMinor = refundedAmountMinor;
        this.providerVersion = providerVersion;
    }

    public UUID getKillBillPaymentId() {
        return killBillPaymentId;
    }

    public UUID getKillBillTransactionId() {
        return killBillTransactionId;
    }

    public String getProviderPaymentId() {
        return providerPaymentId;
    }

    public KillBillOperation getOperation() {
        return operation;
    }

    public KillBillAdapterStatus getStatus() {
        return status;
    }

    public String getProviderStatus() {
        return providerStatus;
    }

    public long getAmountMinor() {
        return amountMinor;
    }

    public String getCurrency() {
        return currency;
    }

    public long getAuthorizedAmountMinor() {
        return authorizedAmountMinor;
    }

    public long getCapturedAmountMinor() {
        return capturedAmountMinor;
    }

    public long getRefundedAmountMinor() {
        return refundedAmountMinor;
    }

    public long getProviderVersion() {
        return providerVersion;
    }
}
