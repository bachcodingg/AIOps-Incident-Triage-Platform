-- Two million reviews over the 40 seeded products: enough that a lookup without
-- idx_review_product (fault F12) costs a sequential scan of ~100 MB, while the
-- index answers it from about 50 000 index entries. PostgreSQL only: the unit
-- tests run on H2 without this location (application.properties).
INSERT INTO review (product_id, stars)
SELECT 1 + (g % 40), 1 + ((g * 7 + g / 40) % 5)
FROM generate_series(1, 2000000) AS g;

ANALYZE review;
