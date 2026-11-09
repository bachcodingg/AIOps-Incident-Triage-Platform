package dev.incidentlens.catalog;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import io.agroal.api.AgroalDataSource;
import java.sql.Connection;
import java.util.ArrayDeque;
import java.util.List;
import org.junit.jupiter.api.Test;

class FaultsTest {

    private static Faults faults(double errorRate, long latencyMs, double leakRate, AgroalDataSource ds, Double... draws) {
        var queue = new ArrayDeque<>(List.of(draws));
        return new Faults(errorRate, latencyMs, leakRate, ds, () -> queue.isEmpty() ? 1.0 : queue.poll());
    }

    @Test
    void offByDefault() throws Exception {
        var ds = mock(AgroalDataSource.class);
        var f = faults(0, 0, 0, ds, 0.0, 0.0);
        assertDoesNotThrow(f::beforeRead);
        verify(ds, never()).getConnection();
    }

    @Test
    void rejectsValuesOutOfRange() {
        assertThrows(IllegalArgumentException.class, () -> faults(1.5, 0, 0, null));
        assertThrows(IllegalArgumentException.class, () -> faults(0, -1, 0, null));
        assertThrows(IllegalArgumentException.class, () -> faults(0, 0, -0.1, null));
    }

    @Test
    void errorRateThrowsLikeARealBug() {
        var f = faults(0.05, 0, 0, null, 0.01, 0.5);
        var e = assertThrows(IllegalStateException.class, f::beforeRead);
        assertFalse(e.getMessage().toLowerCase().contains("fault"));
        assertDoesNotThrow(f::beforeRead);
    }

    @Test
    void latencyIsSpentOnEveryRead() {
        var f = faults(0, 120, 0, null);
        long start = System.nanoTime();
        f.beforeRead();
        assertTrue((System.nanoTime() - start) / 1_000_000 >= 115);
    }

    @Test
    void leakKeepsConnectionsAndNeverClosesThem() throws Exception {
        var ds = mock(AgroalDataSource.class);
        var conn = mock(Connection.class);
        when(ds.getConnection()).thenReturn(conn);
        var f = faults(0, 0, 0.1, ds, 0.05, 0.5, 0.01);
        f.beforeRead();
        f.beforeRead();
        f.beforeRead();
        assertEquals(2, f.leakedConnections());
        verify(conn, never()).close();
    }
}
