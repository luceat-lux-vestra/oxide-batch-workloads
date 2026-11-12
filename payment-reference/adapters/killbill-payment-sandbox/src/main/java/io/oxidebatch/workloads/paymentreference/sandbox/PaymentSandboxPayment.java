package io.oxidebatch.workloads.paymentreference.sandbox;

import com.fasterxml.jackson.annotation.JsonProperty;

public final class PaymentSandboxPayment {
    @JsonProperty("id")
    private String id;

    @JsonProperty("amount")
    private long amount;

    @JsonProperty("currency")
    private String currency;

    @JsonProperty("status")
    private String status;

    @JsonProperty("authorized_amount")
    private long authorizedAmount;

    @JsonProperty("captured_amount")
    private long capturedAmount;

    @JsonProperty("refunded_amount")
    private long refundedAmount;

    @JsonProperty("version")
    private long version;

    public PaymentSandboxPayment() {
    }

    public PaymentSandboxPayment(String id,
                                 long amount,
                                 String currency,
                                 String status,
                                 long authorizedAmount,
                                 long capturedAmount,
                                 long refundedAmount,
                                 long version) {
        this.id = id;
        this.amount = amount;
        this.currency = currency;
        this.status = status;
        this.authorizedAmount = authorizedAmount;
        this.capturedAmount = capturedAmount;
        this.refundedAmount = refundedAmount;
        this.version = version;
    }

    public String getId() {
        return id;
    }

    public long getAmount() {
        return amount;
    }

    public String getCurrency() {
        return currency;
    }

    public String getStatus() {
        return status;
    }

    public long getAuthorizedAmount() {
        return authorizedAmount;
    }

    public long getCapturedAmount() {
        return capturedAmount;
    }

    public long getRefundedAmount() {
        return refundedAmount;
    }

    public long getVersion() {
        return version;
    }
}
