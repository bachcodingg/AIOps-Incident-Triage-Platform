package dev.incidentlens.catalog;

import io.quarkus.panache.common.Page;
import io.quarkus.panache.common.Sort;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.transaction.Transactional;
import java.util.List;
import java.util.Optional;

@ApplicationScoped
public class ProductService {

    public List<Product> list(String category, int page, int size) {
        var query = category == null || category.isBlank()
                ? Product.<Product>findAll(Sort.by("id"))
                : Product.<Product>find("category", Sort.by("id"), category);
        return query.page(Page.of(page, size)).list();
    }

    public Optional<Product> get(long id) {
        return Product.findByIdOptional(id);
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
