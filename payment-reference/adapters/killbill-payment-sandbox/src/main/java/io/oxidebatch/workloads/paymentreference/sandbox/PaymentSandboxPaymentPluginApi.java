package io.oxidebatch.workloads.paymentreference.sandbox;

import java.math.BigDecimal;
import java.sql.SQLException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.UUID;
import java.util.function.Supplier;

import org.joda.time.DateTime;
import org.killbill.billing.catalog.api.Currency;
import org.killbill.billing.payment.api.PaymentMethodPlugin;
import org.killbill.billing.payment.api.PluginProperty;
import org.killbill.billing.payment.api.TransactionType;
import org.killbill.billing.payment.plugin.api.GatewayNotification;
import org.killbill.billing.payment.plugin.api.HostedPaymentPageFormDescriptor;
import org.killbill.billing.payment.plugin.api.PaymentMethodInfoPlugin;
import org.killbill.billing.payment.plugin.api.PaymentPluginApi;
import org.killbill.billing.payment.plugin.api.PaymentPluginApiException;
import org.killbill.billing.payment.plugin.api.PaymentPluginStatus;
import org.killbill.billing.payment.plugin.api.PaymentTransactionInfoPlugin;
import org.killbill.billing.plugin.api.payment.PluginPaymentTransactionInfoPlugin;
import org.killbill.billing.plugin.util.KillBillMoney;
import org.killbill.billing.util.callcontext.CallContext;
import org.killbill.billing.util.callcontext.TenantContext;
import org.killbill.billing.util.entity.Pagination;

public final class PaymentSandboxPaymentPluginApi implements PaymentPluginApi {
    private static final String ERROR_TYPE_UNSUPPORTED = "UNSUPPORTED";

    private final KillBillPaymentAdapter adapter;
    private final PaymentSandboxDao dao;
    private final Supplier<DateTime> now;

    public PaymentSandboxPaymentPluginApi(KillBillPaymentAdapter adapter,
                                          PaymentSandboxDao dao,
                                          Supplier<DateTime> now) {
        this.adapter = adapter;
        this.dao = dao;
        this.now = now;
    }

    @Override
    public PaymentTransactionInfoPlugin authorizePayment(UUID kbAccountId, UUID kbPaymentId, UUID kbTransactionId,
                                                         UUID kbPaymentMethodId, BigDecimal amount, Currency currency,
                                                         Iterable<PluginProperty> properties, CallContext context)
            throws PaymentPluginApiException {
        return execute(kbAccountId, kbPaymentId, kbTransactionId, TransactionType.AUTHORIZE, amount, currency,
                context, () -> adapter.authorize(kbPaymentId, kbTransactionId, minor(amount, currency), currency.name()),
                KillBillPaymentAdapter.operationIdempotencyKey(kbTransactionId, "authorize"), null);
    }

    @Override
    public PaymentTransactionInfoPlugin capturePayment(UUID kbAccountId, UUID kbPaymentId, UUID kbTransactionId,
                                                       UUID kbPaymentMethodId, BigDecimal amount, Currency currency,
                                                       Iterable<PluginProperty> properties, CallContext context)
            throws PaymentPluginApiException {
        return execute(kbAccountId, kbPaymentId, kbTransactionId, TransactionType.CAPTURE, amount, currency,
                context, () -> adapter.capture(kbPaymentId, kbTransactionId, minor(amount, currency), currency.name()),
                KillBillPaymentAdapter.operationIdempotencyKey(kbTransactionId, "capture"), null);
    }

    @Override
    public PaymentTransactionInfoPlugin purchasePayment(UUID kbAccountId, UUID kbPaymentId, UUID kbTransactionId,
                                                        UUID kbPaymentMethodId, BigDecimal amount, Currency currency,
                                                        Iterable<PluginProperty> properties, CallContext context)
            throws PaymentPluginApiException {
        return execute(kbAccountId, kbPaymentId, kbTransactionId, TransactionType.PURCHASE, amount, currency,
                context, () -> adapter.purchase(kbPaymentId, kbTransactionId, minor(amount, currency), currency.name()),
                KillBillPaymentAdapter.operationIdempotencyKey(kbTransactionId, "purchase-authorize"),
                KillBillPaymentAdapter.operationIdempotencyKey(kbTransactionId, "purchase-capture"));
    }

    @Override
    public PaymentTransactionInfoPlugin voidPayment(UUID kbAccountId, UUID kbPaymentId, UUID kbTransactionId,
                                                    UUID kbPaymentMethodId, Iterable<PluginProperty> properties,
                                                    CallContext context) throws PaymentPluginApiException {
        return execute(kbAccountId, kbPaymentId, kbTransactionId, TransactionType.VOID, null, null,
                context, () -> adapter.voidPayment(kbPaymentId, kbTransactionId),
                KillBillPaymentAdapter.operationIdempotencyKey(kbTransactionId, "void"), null);
    }

    @Override
    public PaymentTransactionInfoPlugin refundPayment(UUID kbAccountId, UUID kbPaymentId, UUID kbTransactionId,
                                                      UUID kbPaymentMethodId, BigDecimal amount, Currency currency,
                                                      Iterable<PluginProperty> properties, CallContext context)
            throws PaymentPluginApiException {
        return execute(kbAccountId, kbPaymentId, kbTransactionId, TransactionType.REFUND, amount, currency,
                context, () -> adapter.refund(kbPaymentId, kbTransactionId, minor(amount, currency), currency.name()),
                KillBillPaymentAdapter.operationIdempotencyKey(kbTransactionId, "refund"), null);
    }

    @Override
    public PaymentTransactionInfoPlugin creditPayment(UUID kbAccountId, UUID kbPaymentId, UUID kbTransactionId,
                                                      UUID kbPaymentMethodId, BigDecimal amount, Currency currency,
                                                      Iterable<PluginProperty> properties, CallContext context)
            throws PaymentPluginApiException {
        throw unsupported("creditPayment");
    }

    @Override
    public List<PaymentTransactionInfoPlugin> getPaymentInfo(UUID kbAccountId, UUID kbPaymentId,
                                                             Iterable<PluginProperty> properties, TenantContext context)
            throws PaymentPluginApiException {
        try {
            List<PaymentSandboxTransactionRecord> records = dao.findByPayment(kbPaymentId, context.getTenantId());
            List<PaymentTransactionInfoPlugin> result = new ArrayList<>(records.size());
            for (PaymentSandboxTransactionRecord record : records) {
                result.add(toPluginInfo(record));
            }
            return result;
        } catch (SQLException e) {
            throw new PaymentPluginApiException("Unable to read payment-sandbox transaction journal", e);
        }
    }

    @Override
    public Pagination<PaymentTransactionInfoPlugin> searchPayments(String searchKey, Long offset, Long limit,
                                                                   Iterable<PluginProperty> properties,
                                                                   TenantContext context)
            throws PaymentPluginApiException {
        throw unsupported("searchPayments");
    }

    @Override
    public void addPaymentMethod(UUID kbAccountId, UUID kbPaymentMethodId, PaymentMethodPlugin paymentMethodProps,
                                 boolean setDefault, Iterable<PluginProperty> properties, CallContext context) {
        // payment-sandbox has no provider-side payment-method resource.
    }

    @Override
    public void deletePaymentMethod(UUID kbAccountId, UUID kbPaymentMethodId, Iterable<PluginProperty> properties,
                                    CallContext context) {
        // No provider-side payment-method resource exists.
    }

    @Override
    public PaymentMethodPlugin getPaymentMethodDetail(UUID kbAccountId, UUID kbPaymentMethodId,
                                                      Iterable<PluginProperty> properties, TenantContext context) {
        return new ReferencePaymentMethodPlugin(kbPaymentMethodId, false);
    }

    @Override
    public void setDefaultPaymentMethod(UUID kbAccountId, UUID kbPaymentMethodId,
                                        Iterable<PluginProperty> properties, CallContext context) {
        // Kill Bill remains the payment-method system of record for this synthetic provider.
    }

    @Override
    public List<PaymentMethodInfoPlugin> getPaymentMethods(UUID kbAccountId, boolean refreshFromGateway,
                                                           Iterable<PluginProperty> properties, CallContext context) {
        return Collections.emptyList();
    }

    @Override
    public Pagination<PaymentMethodPlugin> searchPaymentMethods(String searchKey, Long offset, Long limit,
                                                                Iterable<PluginProperty> properties,
                                                                TenantContext context)
            throws PaymentPluginApiException {
        throw unsupported("searchPaymentMethods");
    }

    @Override
    public void resetPaymentMethods(UUID kbAccountId, List<PaymentMethodInfoPlugin> paymentMethods,
                                    Iterable<PluginProperty> properties, CallContext context) {
        // There is no provider-side payment-method inventory to reset.
    }

    @Override
    public HostedPaymentPageFormDescriptor buildFormDescriptor(UUID kbAccountId,
                                                               Iterable<PluginProperty> customFields,
                                                               Iterable<PluginProperty> properties,
                                                               CallContext context)
            throws PaymentPluginApiException {
        throw unsupported("buildFormDescriptor");
    }

    @Override
    public GatewayNotification processNotification(String notification, Iterable<PluginProperty> properties,
                                                   CallContext context)
            throws PaymentPluginApiException {
        throw unsupported("processNotification");
    }

    private PaymentTransactionInfoPlugin execute(UUID kbAccountId,
                                                 UUID kbPaymentId,
                                                 UUID kbTransactionId,
                                                 TransactionType transactionType,
                                                 BigDecimal amount,
                                                 Currency currency,
                                                 CallContext context,
                                                 AdapterCall call,
                                                 String expectedIdempotencyKey,
                                                 String expectedSecondaryIdempotencyKey)
            throws PaymentPluginApiException {
        UUID tenantId = context.getTenantId();
        try {
            PaymentSandboxTransactionRecord existing = dao.findByTransaction(kbTransactionId, tenantId);
            if (existing != null) {
                assertReplayIdentity(existing, kbAccountId, kbPaymentId, transactionType, amount, currency);
                if (existing.getPluginStatus() != PaymentPluginStatus.UNDEFINED) {
                    return toPluginInfo(existing);
                }
            }

            KillBillPaymentResult adapterResult = call.execute();
            PaymentSandboxTransactionRecord record = successRecord(
                    kbAccountId,
                    kbPaymentId,
                    kbTransactionId,
                    transactionType,
                    amount,
                    currency,
                    tenantId,
                    expectedIdempotencyKey,
                    expectedSecondaryIdempotencyKey,
                    adapterResult,
                    existing == null ? now.get() : existing.getCreatedDate());
            dao.save(record);
            return toPluginInfo(record);
        } catch (PaymentSandboxException e) {
            DateTime timestamp = now.get();
            PaymentPluginStatus status = e.isAmbiguous()
                    ? PaymentPluginStatus.UNDEFINED
                    : PaymentPluginStatus.ERROR;
            PaymentSandboxTransactionRecord record = new PaymentSandboxTransactionRecord(
                    kbAccountId,
                    kbPaymentId,
                    kbTransactionId,
                    transactionType,
                    amount,
                    currency,
                    status,
                    e.getProviderPaymentId() == null
                            ? KillBillPaymentAdapter.providerPaymentId(kbPaymentId)
                            : e.getProviderPaymentId(),
                    KillBillPaymentAdapter.createIdempotencyKey(kbPaymentId),
                    expectedIdempotencyKey,
                    expectedSecondaryIdempotencyKey,
                    null,
                    0L,
                    e.getMessage(),
                    e.getErrorCode(),
                    e.isRetryable(),
                    e.isAmbiguous(),
                    timestamp,
                    timestamp,
                    tenantId);
            try {
                dao.save(record);
            } catch (SQLException sqlException) {
                throw new PaymentPluginApiException(
                        "Unable to persist payment-sandbox failure identity",
                        sqlException);
            }
            return toPluginInfo(record);
        } catch (SQLException e) {
            throw new PaymentPluginApiException(
                    "Unable to persist payment-sandbox transaction journal",
                    e);
        } catch (RuntimeException e) {
            throw new PaymentPluginApiException(
                    "payment-sandbox adapter invariant failure",
                    e);
        }
    }

    private PaymentSandboxTransactionRecord successRecord(UUID kbAccountId,
                                                           UUID kbPaymentId,
                                                           UUID kbTransactionId,
                                                           TransactionType transactionType,
                                                           BigDecimal amount,
                                                           Currency currency,
                                                           UUID tenantId,
                                                           String idempotencyKey,
                                                           String secondaryIdempotencyKey,
                                                           KillBillPaymentResult result,
                                                           DateTime createdDate) {
        DateTime updatedDate = now.get();
        return new PaymentSandboxTransactionRecord(
                kbAccountId,
                kbPaymentId,
                kbTransactionId,
                transactionType,
                amount,
                currency,
                pluginStatus(result.getStatus()),
                result.getProviderPaymentId(),
                KillBillPaymentAdapter.createIdempotencyKey(kbPaymentId),
                idempotencyKey,
                secondaryIdempotencyKey,
                result.getProviderStatus(),
                result.getProviderVersion(),
                null,
                null,
                false,
                false,
                createdDate,
                updatedDate,
                tenantId);
    }

    private static PaymentTransactionInfoPlugin toPluginInfo(PaymentSandboxTransactionRecord record) {
        List<PluginProperty> properties = new ArrayList<>();
        properties.add(new PluginProperty(
                "providerStatus",
                record.getProviderStatus(),
                false));
        properties.add(new PluginProperty(
                "providerVersion",
                record.getProviderVersion(),
                false));
        properties.add(new PluginProperty(
                "providerCreateIdempotencyKey",
                record.getProviderCreateIdempotencyKey(),
                false));
        properties.add(new PluginProperty(
                "providerIdempotencyKey",
                record.getProviderIdempotencyKey(),
                false));
        properties.add(new PluginProperty(
                "providerSecondaryIdempotencyKey",
                record.getProviderSecondaryIdempotencyKey(),
                false));
        properties.add(new PluginProperty(
                "retryable",
                record.isRetryable(),
                false));
        properties.add(new PluginProperty(
                "ambiguous",
                record.isAmbiguous(),
                false));

        return new PluginPaymentTransactionInfoPlugin(
                record.getKbPaymentId(),
                record.getKbTransactionId(),
                record.getTransactionType(),
                record.getAmount(),
                record.getCurrency(),
                record.getPluginStatus(),
                record.getGatewayError(),
                record.getGatewayErrorCode(),
                record.getProviderPaymentId(),
                record.getProviderIdempotencyKey(),
                record.getCreatedDate(),
                record.getUpdatedDate(),
                properties);
    }

    private static void assertReplayIdentity(PaymentSandboxTransactionRecord existing,
                                             UUID kbAccountId,
                                             UUID kbPaymentId,
                                             TransactionType transactionType,
                                             BigDecimal amount,
                                             Currency currency) throws PaymentPluginApiException {
        boolean amountMatches = existing.getAmount() == null
                ? amount == null
                : amount != null && existing.getAmount().compareTo(amount) == 0;
        if (!existing.getKbAccountId().equals(kbAccountId)
                || !existing.getKbPaymentId().equals(kbPaymentId)
                || existing.getTransactionType() != transactionType
                || !amountMatches
                || existing.getCurrency() != currency) {
            throw new PaymentPluginApiException(
                    "IDEMPOTENCY_CONFLICT",
                    "Kill Bill transaction replay does not match the persisted canonical mutation");
        }
    }

    private static PaymentPluginStatus pluginStatus(KillBillAdapterStatus status) {
        switch (status) {
            case PROCESSED:
                return PaymentPluginStatus.PROCESSED;
            case PENDING:
                return PaymentPluginStatus.PENDING;
            case CANCELED:
                // A provider-side cancellation is successful completion of
                // Kill Bill's VOID transaction, not cancellation of the plugin call.
                return PaymentPluginStatus.PROCESSED;
            case ERROR:
                return PaymentPluginStatus.ERROR;
            default:
                return PaymentPluginStatus.UNDEFINED;
        }
    }

    private static long minor(BigDecimal amount, Currency currency) {
        if (amount == null || currency == null) {
            throw new IllegalArgumentException("amount and currency are required");
        }
        return KillBillMoney.toMinorUnits(currency.name(), amount);
    }

    private static PaymentPluginApiException unsupported(String operation) {
        return new PaymentPluginApiException(
                ERROR_TYPE_UNSUPPORTED,
                operation + " is not supported by the payment-sandbox reference plugin");
    }

    @FunctionalInterface
    private interface AdapterCall {
        KillBillPaymentResult execute();
    }
}
