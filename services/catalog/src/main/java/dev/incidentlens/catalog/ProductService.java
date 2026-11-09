package dev.incidentlens.catalog;

import io.quarkus.panache.common.Page;
import io.quarkus.panache.common.Sort;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.persistence.EntityManager;
import jakarta.transaction.Transactional;
import java.util.List;
import java.util.Optional;

@ApplicationScoped
public class ProductService {

    private final EntityManager em;

    public ProductService(EntityManager em) {
        this.em = em;
    }

    public List<Product> list(String category, int page, int size) {
        var query = category == null || category.isBlank()
                ? Product.<Product>findAll(Sort.by("id"))
                : Product.<Product>find("category", Sort.by("id"), category);
        return query.page(Page.of(page, size)).list();
    }

    public Optional<Product> get(long id) {
        return Product.<Product>findByIdOptional(id).map(this::withRating);
    }

    // One aggregate per lookup. idx_review_product makes it an index-only scan;
    // without it (fault F12) PostgreSQL reads the whole table every time.
    private Product withRating(Product product) {
        var row = (Object[]) em.createNativeQuery("SELECT count(*), avg(stars) FROM review WHERE product_id = :id")
                .setParameter("id", product.id)
                .getSingleResult();
        product.reviewCount = ((Number) row[0]).longValue();
        product.rating = row[1] == null ? null : Math.round(((Number) row[1]).doubleValue() * 100) / 100.0;
        return product;
    }

    @Transactional
    public Product create(ProductInput input) {
        var product = new Product();
        product.apply(input);
        product.persist();
        return product;
    }

    @Transactional
    public Optional<Product> update(long id, ProductInput input) {
        return Product.<Product>findByIdOptional(id).map(product -> {
            product.apply(input);
            product.flush();
            return product;
        });
    }

    @Transactional
    public boolean delete(long id) {
        return Product.deleteById(id);
    }
}
