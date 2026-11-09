package dev.incidentlens.catalog;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.sdk.metrics.SdkMeterProvider;
import io.opentelemetry.sdk.metrics.data.HistogramPointData;
import io.opentelemetry.sdk.metrics.data.MetricData;
import io.opentelemetry.sdk.testing.exporter.InMemoryMetricReader;
import jakarta.ws.rs.container.ContainerRequestContext;
import jakarta.ws.rs.container.ContainerResponseContext;
import jakarta.ws.rs.container.ResourceInfo;
import java.util.HashMap;
import java.util.Map;
import org.junit.jupiter.api.Test;

class HttpMetricsFilterTest {

    @Test
    void routeComesFromClassAndMethodPaths() throws Exception {
        assertEquals("/products/{id}",
                HttpMetricsFilter.route(ProductResource.class, ProductResource.class.getMethod("get", long.class)));
        assertEquals("/products", HttpMetricsFilter.route(ProductResource.class,
                ProductResource.class.getMethod("list", String.class, int.class, int.class)));
    }

    @Test
    void recordsDurationWithSemconvAttributesAndTheSloBucket() throws Exception {
        InMemoryMetricReader reader = InMemoryMetricReader.create();
        SdkMeterProvider provider = SdkMeterProvider.builder().registerMetricReader(reader).build();
        HttpMetricsFilter filter = new HttpMetricsFilter(provider.get("test"));

        ResourceInfo info = mock(ResourceInfo.class);
        when(info.getResourceClass()).thenAnswer(i -> ProductResource.class);
        when(info.getResourceMethod()).thenReturn(ProductResource.class.getMethod("get", long.class));
        filter.resourceInfo = info;

        Map<String, Object> props = new HashMap<>();
        ContainerRequestContext request = mock(ContainerRequestContext.class);
        when(request.getMethod()).thenReturn("GET");
        when(request.getProperty(org.mockito.ArgumentMatchers.anyString())).thenAnswer(i -> props.get(i.getArgument(0)));
        org.mockito.Mockito.doAnswer(i -> props.put(i.getArgument(0), i.getArgument(1)))
                .when(request).setProperty(org.mockito.ArgumentMatchers.anyString(), org.mockito.ArgumentMatchers.any());
        ContainerResponseContext response = mock(ContainerResponseContext.class);
        when(response.getStatus()).thenReturn(404);

        filter.filter(request);
        filter.filter(request, response);

        MetricData metric = reader.collectAllMetrics().iterator().next();
        assertEquals("http.server.request.duration", metric.getName());
        assertEquals("s", metric.getUnit());
        HistogramPointData point = metric.getHistogramData().getPoints().iterator().next();
        assertEquals(1, point.getCount());
        assertTrue(point.getBoundaries().contains(0.3), "0.3 s, the latency SLO, must be a bucket boundary");
        assertEquals(Attributes.of(
                HttpMetricsFilter.METHOD, "GET",
                HttpMetricsFilter.ROUTE, "/products/{id}",
                HttpMetricsFilter.STATUS, 404L), point.getAttributes());
    }
}
