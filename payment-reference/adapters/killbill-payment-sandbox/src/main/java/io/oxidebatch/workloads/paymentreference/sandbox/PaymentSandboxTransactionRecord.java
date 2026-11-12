package io.oxidebatch.workloads.paymentreference.sandbox;

import java.math.BigDecimal;
import java.util.UUID;

import org.joda.time.DateTime;
import org.killbill.billing.catalog.api.Currency;
import org.killbill.billing.payment.api.TransactionType;
import org.killbill.billing.payment.plugin.api.PaymentPluginStatus;

public final class PaymentSandboxTransactionRecord {
    private final UUID kbAccountId;
    private final UUID kbPaymentId;
    private final UUID kbTransactionId;
    private final TransactionType transactionType;
    private final BigDecimal amount;
    private final Currency currency;
    private final PaymentPluginStatus pluginStatus;
    private final String providerPaymentId;
    private final String providerCreateIdempotencyKey;
    private final String providerIdempotencyKey;
    private final String providerSecondaryIdempotencyKey;
    private final String providerStatus;
    private final long providerVersion;
    private final String gatewayError;
    private final String gatewayErrorCode;
    private final boolean retryable;
    private final boolean ambiguous;
    private final DateTime createdDate;
    private final DateTime updatedDate;
    private final UUID kbTenantId;

    public PaymentSandboxTransactionRecord(UUID kbAccountId,
                                           UUID kbPaymentId,
                                           UUID kbTransactionId,
                                           TransactionType transactionType,
                                           BigDecimal amount,
                                           Currency currency,
                                           PaymentPluginStatus pluginStatus,
                                           String providerPaymentId,
                                           String providerCreateIdempotencyKey,
                                           String providerIdempotencyKey,
                                           String providerSecondaryIdempotencyKey,
                                           String providerStatus,
                                           long providerVersion,
                                           String gatewayError,
                                           String gatewayErrorCode,
                                           boolean retryable,
                                           boolean ambiguous,
                                           DateTime createdDate,
                                           DateTime updatedDate,
                                           UUID kbTenantId) {
        this.kbAccountId = kbAccountId;
        this.kbPaymentId = kbPaymentId;
        this.kbTransactionId = kbTransactionId;
        this.transactionType = transactionType;
        this.amount = amount;
        this.currency = currency;
        this.pluginStatus = pluginStatus;
        this.providerPaymentId = providerPaymentId;
        this.providerCreateIdempotencyKey = providerCreateIdempotencyKey;
        this.providerIdempotencyKey = providerIdempotencyKey;
        this.providerSecondaryIdempotencyKey = providerSecondaryIdempotencyKey;
        this.providerStatus = providerStatus;
        this.providerVersion = providerVersion;
        this.gatewayError = gatewayError;
        this.gatewayErrorCode = gatewayErrorCode;
        this.retryable = retryable;
        this.ambiguous = ambiguous;
        this.createdDate = createdDate;
        this.updatedDate = updatedDate;
        this.kbTenantId = kbTenantId;
    }

    public UUID getKbAccountId() { return kbAccountId; }
    public UUID getKbPaymentId() { return kbPaymentId; }
    public UUID getKbTransactionId() { return kbTransactionId; }
    public TransactionType getTransactionType() { return transactionType; }
    public BigDecimal getAmount() { return amount; }
    public Currency getCurrency() { return currency; }
    public PaymentPluginStatus getPluginStatus() { return pluginStatus; }
    public String getProviderPaymentId() { return providerPaymentId; }
    public String getProviderCreateIdempotencyKey() { return providerCreateIdempotencyKey; }
    public String getProviderIdempotencyKey() { return providerIdempotencyKey; }
    public String getProviderSecondaryIdempotencyKey() { return providerSecondaryIdempotencyKey; }
    public String getProviderStatus() { return providerStatus; }
    public long getProviderVersion() { return providerVersion; }
    public String getGatewayError() { return gatewayError; }
    public String getGatewayErrorCode() { return gatewayErrorCode; }
    public boolean isRetryable() { return retryable; }
    public boolean isAmbiguous() { return ambiguous; }
    public DateTime getCreatedDate() { return createdDate; }
    public DateTime getUpdatedDate() { return updatedDate; }
    public UUID getKbTenantId() { return kbTenantId; }
}
