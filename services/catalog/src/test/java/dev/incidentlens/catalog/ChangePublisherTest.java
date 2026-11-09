package dev.incidentlens.catalog;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.context.Scope;
import io.opentelemetry.sdk.trace.SdkTracerProvider;
import io.quarkus.redis.datasource.RedisDataSource;
import io.quarkus.redis.datasource.list.ListCommands;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;

class ChangePublisherTest {

    @SuppressWarnings("unchecked")
    private final ListCommands<String, String> queue = mock(ListCommands.class);
    private final ObjectMapper mapper = new ObjectMapper();

    private ChangePublisher publisher() {
        RedisDataSource redis = mock(RedisDataSource.class);
        when(redis.list(String.class)).thenReturn(queue);
        return new ChangePublisher(redis, mapper);
    }

    private JsonNode published() throws Exception {
        ArgumentCaptor<String> event = ArgumentCaptor.forClass(String.class);
        verify(queue).lpush(eq(ChangePublisher.QUEUE_KEY), event.capture());
        return mapper.readTree(event.getValue());
    }

    @Test
    void eventCarriesTheTraceOfTheRequestThatMadeTheChange() throws Exception {
        Span span = SdkTracerProvider.builder().build().get("test").spanBuilder("PUT /products/{id}").startSpan();
        try (Scope ignored = span.makeCurrent()) {
            publisher().deleted(7);
        } finally {
            span.end();
        }
        JsonNode event = published();
        assertEquals("delete", event.get("op").asText());
        assertEquals("00-" + span.getSpanContext().getTraceId() + "-" + span.getSpanContext().getSpanId() + "-"
                        + span.getSpanContext().getTraceFlags().asHex(),
                event.get("traceparent").asText());
    }

    @Test
    void eventOutsideATraceHasNoTraceparent() throws Exception {
        publisher().deleted(7);
        assertFalse(published().has("traceparent"));
    }
}
