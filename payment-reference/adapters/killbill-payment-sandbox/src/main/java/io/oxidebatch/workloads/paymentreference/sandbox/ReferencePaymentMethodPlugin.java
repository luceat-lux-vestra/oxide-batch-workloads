package io.oxidebatch.workloads.paymentreference.sandbox;

import java.util.Collections;
import java.util.List;
import java.util.UUID;

import org.killbill.billing.payment.api.PaymentMethodPlugin;
import org.killbill.billing.payment.api.PluginProperty;

public final class ReferencePaymentMethodPlugin implements PaymentMethodPlugin {
    private final UUID kbPaymentMethodId;
    private final boolean defaultPaymentMethod;

    public ReferencePaymentMethodPlugin(UUID kbPaymentMethodId, boolean defaultPaymentMethod) {
        this.kbPaymentMethodId = kbPaymentMethodId;
        this.defaultPaymentMethod = defaultPaymentMethod;
    }

    @Override public UUID getKbPaymentMethodId() { return kbPaymentMethodId; }
    @Override public String getExternalPaymentMethodId() { return null; }
    @Override public boolean isDefaultPaymentMethod() { return defaultPaymentMethod; }
    @Override public List<PluginProperty> getProperties() { return Collections.emptyList(); }
}
