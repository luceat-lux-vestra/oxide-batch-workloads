package io.oxidebatch.workloads.paymentreference.sandbox;

import java.net.URI;
import java.util.Hashtable;

import org.killbill.billing.osgi.api.OSGIPluginProperties;
import org.killbill.billing.osgi.libs.killbill.KillbillActivatorBase;
import org.killbill.billing.payment.plugin.api.PaymentPluginApi;
import org.osgi.framework.BundleContext;

public final class PaymentSandboxActivator extends KillbillActivatorBase {
    public static final String PLUGIN_NAME = "killbill-payment-sandbox";
    public static final String BASE_URI_PROPERTY = "org.killbill.billing.plugin.paymentSandbox.baseUri";

    @Override
    public void start(BundleContext context) throws Exception {
        super.start(context);

        String baseUri = configProperties.getString(BASE_URI_PROPERTY);
        if (baseUri == null || baseUri.trim().isEmpty()) {
            throw new IllegalStateException("Missing required Kill Bill property " + BASE_URI_PROPERTY);
        }

        PaymentSandboxClient client = new HttpPaymentSandboxClient(URI.create(baseUri));
        KillBillPaymentAdapter adapter = new KillBillPaymentAdapter(client);
        PaymentSandboxDao dao = new PaymentSandboxDao(dataSource.getDataSource());
        PaymentPluginApi api = new PaymentSandboxPaymentPluginApi(adapter, dao, () -> clock.getClock().getUTCNow());

        Hashtable<String, String> properties = new Hashtable<>();
        properties.put(OSGIPluginProperties.PLUGIN_NAME_PROP, PLUGIN_NAME);
        registrar.registerService(context, PaymentPluginApi.class, api, properties);
    }
}
