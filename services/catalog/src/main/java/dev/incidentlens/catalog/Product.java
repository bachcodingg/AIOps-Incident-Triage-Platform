package dev.incidentlens.catalog;

import com.fasterxml.jackson.annotation.JsonInclude;
import io.quarkus.hibernate.orm.panache.PanacheEntityBase;
import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.PrePersist;
import jakarta.persistence.PreUpdate;
import jakarta.persistence.Table;
import jakarta.persistence.Transient;
import java.math.BigDecimal;
import java.time.Instant;

@Entity
@Table(name = "product")
public class Product extends PanacheEntityBase {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    public Long id;

    @Column(nullable = false, unique = true)
    public String sku;

    @Column(nullable = false)
    public String name;

    public String description;

    @Column(nullable = false)
    public String category;

    @Column(nullable = false)
    public BigDecimal price;

    @Column(nullable = false)
    public int stock;

    @Column(name = "updated_at", nullable = false)
    public Instant updatedAt;

    // Filled in for a single-product lookup only, from the review table, so a
    // listing (and the indexer's full reindex) looks exactly as before.
    @Transient
    @JsonInclude(JsonInclude.Include.NON_NULL)
    public Double rating;

    @Transient
    @JsonInclude(JsonInclude.Include.NON_NULL)
    public Long reviewCount;

    @PrePersist
    @PreUpdate
    void touch() {
        updatedAt = Instant.now();
    }

    void apply(ProductInput input) {
        sku = input.sku();
        name = input.name();
        description = input.description();
        category = input.category();
        price = input.price();
        stock = input.stock();
    }
}
