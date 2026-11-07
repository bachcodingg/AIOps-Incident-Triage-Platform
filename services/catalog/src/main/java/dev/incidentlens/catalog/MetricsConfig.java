package dev.incidentlens.catalog;

import io.micrometer.core.instrument.Meter;
import io.micrometer.core.instrument.config.MeterFilter;
import io.micrometer.core.instrument.distribution.DistributionStatisticConfig;
import jakarta.enterprise.inject.Produces;
import jakarta.inject.Singleton;

/** Histogram buckets for request latency, so Prometheus can compute a p95 during canary analysis. */
@Singleton
public class MetricsConfig {

    private static final double[] BUCKETS_SECONDS = {0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1, 2.5};

    @Produces
    @Singleton
    MeterFilter httpLatencyBuckets() {
        return new MeterFilter() {
            @Override
            public DistributionStatisticConfig configure(Meter.Id id, DistributionStatisticConfig config) {
                if (!id.getName().equals("http.server.requests")) {
                    return config;
                }
                double[] nanos = new double[BUCKETS_SECONDS.length];
                for (int i = 0; i < nanos.length; i++) {
                    nanos[i] = BUCKETS_SECONDS[i] * 1e9;
                }
                return DistributionStatisticConfig.builder().serviceLevelObjectives(nanos).build().merge(config);
            }
        };
    }
}
