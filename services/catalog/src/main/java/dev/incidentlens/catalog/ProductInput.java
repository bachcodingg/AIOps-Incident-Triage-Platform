package dev.incidentlens.catalog;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.PositiveOrZero;
import jakarta.validation.constraints.Size;
import java.math.BigDecimal;

/** Request body for create and update. Keeps id and timestamps out of client control. */
public record ProductInput(
        @NotBlank @Size(max = 32) String sku,
        @NotBlank @Size(max = 200) String name,
        @Size(max = 2000) String description,
        @NotBlank @Size(max = 64) String category,
        @NotNull @PositiveOrZero BigDecimal price,
        @PositiveOrZero int stock) {
}
