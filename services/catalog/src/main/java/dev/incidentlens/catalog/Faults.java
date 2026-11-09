package dev.incidentlens.catalog;

import io.agroal.api.AgroalDataSource;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.inject.Inject;
import java.sql.Connection;
import java.sql.SQLException;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.function.DoubleSupplier;
import org.eclipse.microprofile.config.inject.ConfigProperty;

/**
 * Chaos flags for deliberately bad releases (chaos/catalog.yaml). A bad release
 * is the normal image with one of these baked in as an environment variable, so
 * the release history shows only a new image tag, and the fault behaves like a
 * real bug: nothing in a response or a log line says it was injected.
 *
 * <ul>
 *   <li>FAULT_ERROR_RATE: F02, share of reads that throw
 *   <li>FAULT_LATENCY_MS: F03, extra time spent on every read
 *   <li>FAULT_CONNECTION_LEAK_RATE: F11, share of lookups that take a database
 *       connection and never give it back, until the pool is exhausted
 *   <li>FAULT_DROP_REVIEW_INDEX: F12, see {@link ReviewIndex}
 * </ul>
 */
@ApplicationScoped
public class Faults {

    private final double errorRate;
    private final long latencyMs;
    private final double leakRate;
    private final AgroalDataSource dataSource;
    private final DoubleSupplier random;
    private final List<Connection> leaked = new CopyOnWriteArrayList<>();

    @Inject
    public Faults(
            @ConfigProperty(name = "fault.error-rate", defaultValue = "0") double errorRate,
            @ConfigProperty(name = "fault.latency-ms", defaultValue = "0") long latencyMs,
            @ConfigProperty(name = "fault.connection-leak-rate", defaultValue = "0") double leakRate,
            AgroalDataSource dataSource) {
        this(errorRate, latencyMs, leakRate, dataSource, Math::random);
    }

    Faults(double errorRate, long latencyMs, double leakRate, AgroalDataSource dataSource, DoubleSupplier random) {
        requireShare("FAULT_ERROR_RATE", errorRate);
        requireShare("FAULT_CONNECTION_LEAK_RATE", leakRate);
        if (latencyMs < 0) {
            throw new IllegalArgumentException("FAULT_LATENCY_MS must be 0 or more, got " + latencyMs);
        }
        this.errorRate = errorRate;
        this.latencyMs = latencyMs;
        this.leakRate = leakRate;
        this.dataSource = dataSource;
        this.random = random;
    }

    private static void requireShare(String name, double value) {
        if (value < 0 || value > 1) {
            throw new IllegalArgumentException(name + " must be between 0 and 1, got " + value);
        }
    }

    /** Called at the start of every read endpoint. */
    void beforeRead() {
        if (latencyMs > 0) {
            try {
                Thread.sleep(latencyMs);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }
        if (leakRate > 0 && random.getAsDouble() < leakRate) {
            try {
                // An early return that skips close(): the pool never gets it back.
                leaked.add(dataSource.getConnection());
            } catch (SQLException e) {
                throw new IllegalStateException("could not load price rules", e);
            }
        }
        if (errorRate > 0 && random.getAsDouble() < errorRate) {
            throw new IllegalStateException("price rules not loaded for currency EUR");
        }
    }

    int leakedConnections() {
        return leaked.size();
    }
}
