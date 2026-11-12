package io.oxidebatch.workloads.paymentreference.sandbox;

import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

class KillBillPaymentAdapterTest {

    @Test
    void authorizeUsesDeterministicIdentityAndStableMutationKeys() {
        FakeClient client = new FakeClient();
        KillBillPaymentAdapter adapter = new KillBillPaymentAdapter(client);
        UUID paymentId = UUID.fromString("11111111-1111-1111-1111-111111111111");
        UUID transactionId = UUID.fromString("22222222-2222-2222-2222-222222222222");

        KillBillPaymentResult result = adapter.authorize(paymentId, transactionId, 10_000L, "usd");

        assertEquals("kb-11111111-1111-1111-1111-111111111111", result.getProviderPaymentId());
        assertEquals(KillBillOperation.AUTHORIZE, result.getOperation());
        assertEquals(KillBillAdapterStatus.PROCESSED, result.getStatus());
        assertEquals(List.of(
                "create|kb-11111111-1111-1111-1111-111111111111|10000|USD|kb-payment:11111111-1111-1111-1111-111111111111:create|kb-txn:22222222-2222-2222-2222-222222222222",
                "authorize|kb-11111111-1111-1111-1111-111111111111|kb-txn:22222222-2222-2222-2222-222222222222:authorize|kb-txn:22222222-2222-2222-2222-222222222222"
        ), client.calls);
    }

    @Test
    void purchaseUsesTwoDistinctIdempotentProviderMutations() {
        FakeClient client = new FakeClient();
        KillBillPaymentAdapter adapter = new KillBillPaymentAdapter(client);
        UUID paymentId = UUID.fromString("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa");
        UUID transactionId = UUID.fromString("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb");

        KillBillPaymentResult result = adapter.purchase(paymentId, transactionId, 5_000L, "EUR");

        assertEquals(KillBillOperation.PURCHASE, result.getOperation());
        assertEquals("captured", result.getProviderStatus());
        assertEquals(List.of(
                "create|kb-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa|5000|EUR|kb-payment:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa:create|kb-txn:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "authorize|kb-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa|kb-txn:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb:purchase-authorize|kb-txn:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "capture|kb-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa|5000|EUR|kb-txn:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb:purchase-capture|kb-txn:bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        ), client.calls);
    }

    @Test
    void sameKillBillTransactionGeneratesSameProviderIdempotencyKeyOnReplay() {
        FakeClient client = new FakeClient();
        KillBillPaymentAdapter adapter = new KillBillPaymentAdapter(client);
        UUID paymentId = UUID.randomUUID();
        UUID transactionId = UUID.randomUUID();

        adapter.authorize(paymentId, transactionId, 100L, "USD");
        List<String> first = new ArrayList<>(client.calls);
        client.calls.clear();
        adapter.authorize(paymentId, transactionId, 100L, "USD");

        assertEquals(first, client.calls);
    }

    @Test
    void createConflictConvergesThroughStatusReadWhenImmutableIdentityMatches() {
        FakeClient client = new FakeClient();
        client.conflictOnCreate = true;
        KillBillPaymentAdapter adapter = new KillBillPaymentAdapter(client);
        UUID paymentId = UUID.randomUUID();
        UUID transactionId = UUID.randomUUID();

        KillBillPaymentResult result = adapter.authorize(paymentId, transactionId, 250L, "USD");

        assertEquals(KillBillAdapterStatus.PROCESSED, result.getStatus());
        assertTrue(client.calls.get(1).startsWith("get|"));
        assertTrue(client.calls.get(2).startsWith("authorize|"));
    }

    @Test
    void createConflictFailsClosedWhenProviderIdentityHasDifferentMoney() {
        FakeClient client = new FakeClient();
        client.conflictOnCreate = true;
        client.existingAmount = 999L;
        KillBillPaymentAdapter adapter = new KillBillPaymentAdapter(client);

        assertThrows(IllegalStateException.class,
                () -> adapter.authorize(UUID.randomUUID(), UUID.randomUUID(), 250L, "USD"));
    }

    @Test
    void statusMappingMatchesPinnedPaymentSandboxStates() {
        assertEquals(KillBillAdapterStatus.PENDING, KillBillAdapterStatus.fromProviderStatus("pending"));
        assertEquals(KillBillAdapterStatus.PROCESSED, KillBillAdapterStatus.fromProviderStatus("authorized"));
        assertEquals(KillBillAdapterStatus.PROCESSED, KillBillAdapterStatus.fromProviderStatus("partially_captured"));
        assertEquals(KillBillAdapterStatus.PROCESSED, KillBillAdapterStatus.fromProviderStatus("captured"));
        assertEquals(KillBillAdapterStatus.PROCESSED, KillBillAdapterStatus.fromProviderStatus("partially_refunded"));
        assertEquals(KillBillAdapterStatus.PROCESSED, KillBillAdapterStatus.fromProviderStatus("refunded"));
        assertEquals(KillBillAdapterStatus.CANCELED, KillBillAdapterStatus.fromProviderStatus("cancelled"));
        assertEquals(KillBillAdapterStatus.ERROR, KillBillAdapterStatus.fromProviderStatus("failed"));
        assertThrows(IllegalArgumentException.class, () -> KillBillAdapterStatus.fromProviderStatus("mystery"));
    }

    @Test
    void followUpOperationsKeepPaymentAndTransactionIdentitySeparate() {
        FakeClient client = new FakeClient();
        KillBillPaymentAdapter adapter = new KillBillPaymentAdapter(client);
        UUID paymentId = UUID.fromString("10000000-0000-0000-0000-000000000001");
        UUID captureId = UUID.fromString("20000000-0000-0000-0000-000000000002");
        UUID voidId = UUID.fromString("30000000-0000-0000-0000-000000000003");
        UUID refundId = UUID.fromString("40000000-0000-0000-0000-000000000004");

        adapter.capture(paymentId, captureId, 50L, "USD");
        adapter.voidPayment(paymentId, voidId);
        adapter.refund(paymentId, refundId, 25L, "USD");

        assertTrue(client.calls.get(0).contains("kb-10000000-0000-0000-0000-000000000001"));
        assertTrue(client.calls.get(0).contains("20000000-0000-0000-0000-000000000002:capture"));
        assertTrue(client.calls.get(1).contains("30000000-0000-0000-0000-000000000003:void"));
        assertTrue(client.calls.get(2).contains("40000000-0000-0000-0000-000000000004:refund"));
    }

    private static final class FakeClient implements PaymentSandboxClient {
        private final List<String> calls = new ArrayList<>();
        private boolean conflictOnCreate;
        private long existingAmount = -1;
        private String existingCurrency = "USD";

        @Override
        public PaymentSandboxPayment createPayment(String providerPaymentId,
                                                   long amountMinor,
                                                   String currency,
                                                   String idempotencyKey,
                                                   String correlationId) {
            calls.add("create|" + providerPaymentId + "|" + amountMinor + "|" + currency + "|" + idempotencyKey + "|" + correlationId);
            if (conflictOnCreate) {
                throw PaymentSandboxException.http("payment already exists", 409, "payment_exists", providerPaymentId, idempotencyKey);
            }
            return payment(providerPaymentId, amountMinor, currency, "pending", 0, 0, 0, 1);
        }

        @Override
        public PaymentSandboxPayment authorize(String providerPaymentId, String idempotencyKey, String correlationId) {
            calls.add("authorize|" + providerPaymentId + "|" + idempotencyKey + "|" + correlationId);
            return payment(providerPaymentId, 10_000L, "USD", "authorized", 10_000L, 0, 0, 2);
        }

        @Override
        public PaymentSandboxPayment capture(String providerPaymentId,
                                             long amountMinor,
                                             String currency,
                                             String idempotencyKey,
                                             String correlationId) {
            calls.add("capture|" + providerPaymentId + "|" + amountMinor + "|" + currency + "|" + idempotencyKey + "|" + correlationId);
            return payment(providerPaymentId, amountMinor, currency, "captured", amountMinor, amountMinor, 0, 3);
        }

        @Override
        public PaymentSandboxPayment cancel(String providerPaymentId, String idempotencyKey, String correlationId) {
            calls.add("cancel|" + providerPaymentId + "|" + idempotencyKey + "|" + correlationId);
            return payment(providerPaymentId, 100L, "USD", "cancelled", 100L, 0, 0, 3);
        }

        @Override
        public PaymentSandboxPayment refund(String providerPaymentId,
                                            long amountMinor,
                                            String currency,
                                            String idempotencyKey,
                                            String correlationId) {
            calls.add("refund|" + providerPaymentId + "|" + amountMinor + "|" + currency + "|" + idempotencyKey + "|" + correlationId);
            return payment(providerPaymentId, amountMinor, currency, "refunded", amountMinor, amountMinor, amountMinor, 4);
        }

        @Override
        public PaymentSandboxPayment getPayment(String providerPaymentId, String correlationId) {
            calls.add("get|" + providerPaymentId + "|" + correlationId);
            long amount = existingAmount >= 0 ? existingAmount : 250L;
            return payment(providerPaymentId, amount, existingCurrency, "pending", 0, 0, 0, 1);
        }

        private PaymentSandboxPayment payment(String id,
                                              long amount,
                                              String currency,
                                              String status,
                                              long authorized,
                                              long captured,
                                              long refunded,
                                              long version) {
            return new PaymentSandboxPayment(id, amount, currency, status, authorized, captured, refunded, version);
        }
    }
}
