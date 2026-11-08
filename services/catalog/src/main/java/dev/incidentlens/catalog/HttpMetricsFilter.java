package dev.incidentlens.catalog;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.metrics.DoubleHistogram;
import io.opentelemetry.api.metrics.Meter;
import jakarta.enterprise.inject.Instance;
import jakarta.inject.Inject;
import jakarta.ws.rs.Path;
import jakarta.ws.rs.container.ContainerRequestContext;
import jakarta.ws.rs.container.ContainerRequestFilter;
import jakarta.ws.rs.container.ContainerResponseContext;
import jakarta.ws.rs.container.ContainerResponseFilter;
import jakarta.ws.rs.container.ResourceInfo;
import jakarta.ws.rs.core.Context;
import jakarta.ws.rs.ext.Provider;
import java.lang.reflect.Method;
import java.util.List;
import java.util.function.Supplier;

/**
 * Records http.server.request.duration as the OpenTelemetry HTTP semantic
 * conventions define it, the same metric gateway and search-api produce. In
 * Prometheus it is http_server_request_duration_seconds_*.
 *
 * Quarkus can record this metric itself, but with the semantic-convention
 * buckets, which jump from 0.25 s to 0.5 s. The latency SLO counts requests
 * under 300 ms, so 0.3 s has to be a boundary. Quarkus' own one is switched off
 * in application.properties.
 */
@Provider
public class HttpMetricsFilter implements ContainerRequestFilter, ContainerResponseFilter {

    static final List<Double> BUCKETS_SECONDS = List.of(0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.5, 5.0);

    static final AttributeKey<String> METHOD = AttributeKey.stringKey("http.request.method");
    static final AttributeKey<String> ROUTE = AttributeKey.stringKey("http.route");
    static final AttributeKey<Long> STATUS = AttributeKey.longKey("http.response.status_code");

    private static final String START = HttpMetricsFilter.class.getName() + ".start";

    // JAX-RS creates providers at start-up, before the OpenTelemetry SDK bean
    // exists, so the meter is looked up on the first request instead.
    private final Supplier<Meter> meter;
    private volatile DoubleHistogram duration;

    @Context ResourceInfo resourceInfo;

    @Inject
    public HttpMetricsFilter(Instance<Meter> meter) {
        this.meter = meter::get;
    }

    HttpMetricsFilter(Meter meter) {
        this.meter = () -> meter;
    }

    private DoubleHistogram duration() {
        DoubleHistogram h = duration;
        if (h == null) {
            h = meter.get().histogramBuilder("http.server.request.duration")
                    .setUnit("s")
                    .setDescription("Duration of API requests served by catalog.")
                    .setExplicitBucketBoundariesAdvice(BUCKETS_SECONDS)
                    .build();
            duration = h; // a race builds the same instrument twice, which the SDK deduplicates
        }
        return h;
    }

    @Override
    public void filter(ContainerRequestContext request) {
        request.setProperty(START, System.nanoTime());
    }

    @Override
    public void filter(ContainerRequestContext request, ContainerResponseContext response) {
        // Only requests that matched a resource: no route means nothing to group by.
        if (!(request.getProperty(START) instanceof Long start) || resourceInfo == null) {
            return;
        }
        String route = route(resourceInfo.getResourceClass(), resourceInfo.getResourceMethod());
        if (route == null) {
            return;
        }
        // Recorded inside the server span, so the SDK keeps the trace as an exemplar.
        duration().record(
                (System.nanoTime() - start) / 1e9,
                Attributes.of(METHOD, request.getMethod(), ROUTE, route, STATUS, (long) response.getStatus()));
    }

    /** The route template, e.g. "/products/{id}", from the class and method @Path. */
    static String route(Class<?> resource, Method method) {
        if (resource == null || method == null) {
            return null;
        }
        Path classPath = resource.getAnnotation(Path.class);
        Path methodPath = method.getAnnotation(Path.class);
        String route = join(classPath == null ? "" : classPath.value(), methodPath == null ? "" : methodPath.value());
        return route.isEmpty() ? "/" : route;
    }

    private static String join(String a, String b) {
        String left = a.endsWith("/") ? a.substring(0, a.length() - 1) : a;
        String right = b.isEmpty() || b.startsWith("/") ? b : "/" + b;
        String joined = left + right;
        return joined.startsWith("/") || joined.isEmpty() ? joined : "/" + joined;
    }
}
