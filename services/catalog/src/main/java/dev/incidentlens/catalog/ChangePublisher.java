package dev.incidentlens.catalog;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import io.quarkus.redis.datasource.RedisDataSource;
import io.quarkus.redis.datasource.list.ListCommands;
import jakarta.enterprise.context.ApplicationScoped;
import java.util.LinkedHashMap;
import java.util.Map;
import org.jboss.logging.Logger;

/**
 * Pushes change events onto the Redis list the indexer consumes.
 *
 * Called after the database transaction commits. If Redis is down the write
 * still succeeds and the search index goes stale until the indexer's next
 * full reindex; that trade-off is deliberate and is fault F10 in the catalog.
 */
@ApplicationScoped
public class ChangePublisher {

    static final String QUEUE_KEY = "catalog:changes";
    private static final Logger LOG = Logger.getLogger(ChangePublisher.class);

    private final ListCommands<String, String> queue;
    private final ObjectMapper mapper;

    public ChangePublisher(RedisDataSource redis, ObjectMapper mapper) {
        this.queue = redis.list(String.class);
        this.mapper = mapper;
    }

    public void upserted(Product product) {
        Map<String, Object> event = new LinkedHashMap<>();
        event.put("op", "upsert");
        event.put("id", product.id);
        event.put("product", product);
        push(event);
    }

    public void deleted(long id) {
        push(Map.of("op", "delete", "id", id));
    }

    private void push(Map<String, Object> event) {
        try {
            queue.lpush(QUEUE_KEY, mapper.writeValueAsString(event));
        } catch (JsonProcessingException e) {
            LOG.errorf(e, "could not serialise change event %s", event.get("id"));
        } catch (RuntimeException e) {
            LOG.warnf("could not publish change event for product %s: %s", event.get("id"), e.getMessage());
        }
    }
}
