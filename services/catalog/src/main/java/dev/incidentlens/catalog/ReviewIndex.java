package dev.incidentlens.catalog;

import io.quarkus.runtime.StartupEvent;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.enterprise.event.Observes;
import jakarta.persistence.EntityManager;
import jakarta.transaction.Transactional;
import org.eclipse.microprofile.config.inject.ConfigProperty;
import org.jboss.logging.Logger;

/**
 * Keeps idx_review_product in place, which every product lookup depends on.
 *
 * <p>Fault F12 is a release that drops it, the way a careless migration does.
 * Flyway cannot undo a migration, so a plain rollback would leave the index
 * missing; instead every release puts the index back when it starts, which is
 * the forward fix a real rollback needs. The bad release is the normal image
 * with FAULT_DROP_REVIEW_INDEX=true baked in (chaos/catalog.yaml).
 */
@ApplicationScoped
public class ReviewIndex {

    private static final Logger LOG = Logger.getLogger(ReviewIndex.class);

    private final EntityManager em;
    private final boolean drop;

    public ReviewIndex(EntityManager em, @ConfigProperty(name = "fault.drop-review-index", defaultValue = "false") boolean drop) {
        this.em = em;
        this.drop = drop;
    }

    @Transactional
    void onStart(@Observes StartupEvent event) {
        if (drop) {
            em.createNativeQuery("DROP INDEX IF EXISTS idx_review_product").executeUpdate();
            LOG.info("schema cleanup: removed index idx_review_product");
        } else {
            em.createNativeQuery("CREATE INDEX IF NOT EXISTS idx_review_product ON review (product_id, stars)").executeUpdate();
        }
    }
}
