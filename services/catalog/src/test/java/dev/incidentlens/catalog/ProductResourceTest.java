package dev.incidentlens.catalog;

import static io.restassured.RestAssured.given;
import static org.hamcrest.Matchers.equalTo;
import static org.hamcrest.Matchers.everyItem;
import static org.hamcrest.Matchers.hasKey;
import static org.hamcrest.Matchers.hasSize;
import static org.hamcrest.Matchers.not;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.argThat;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;

import io.quarkus.narayana.jta.QuarkusTransaction;
import io.quarkus.test.InjectMock;
import io.quarkus.test.junit.QuarkusTest;
import io.restassured.http.ContentType;
import jakarta.inject.Inject;
import jakarta.persistence.EntityManager;
import org.junit.jupiter.api.Test;

@QuarkusTest
class ProductResourceTest {

    @InjectMock
    ChangePublisher publisher;

    @Inject
    EntityManager em;

    private static String body(String sku, String price) {
        return """
                {"sku": "%s", "name": "Test Phone", "description": "for tests",
                 "category": "phones", "price": %s, "stock": 3}
                """.formatted(sku, price);
    }

    @Test
    void noPrometheusEndpointMetricsArePushedOverOtlp() {
        given().when().get("/q/metrics").then().statusCode(404);
    }

    @Test
    void seedDataIsServedInPages() {
        given().queryParam("size", 5)
                .when().get("/products")
                .then().statusCode(200).body("$", hasSize(5)).body("[0].sku", equalTo("LT-001"));
    }

    @Test
    void filtersByCategory() {
        given().queryParam("category", "tablets")
                .when().get("/products")
                .then().statusCode(200).body("$", hasSize(3)).body("category", everyItem(equalTo("tablets")));
    }

    @Test
    void lookupShowsTheAverageRatingAndListingsDoNot() {
        QuarkusTransaction.requiringNew().run(() -> {
            for (int stars : new int[] {5, 4, 4}) {
                em.createNativeQuery("INSERT INTO review (product_id, stars) VALUES (7, :stars)")
                        .setParameter("stars", stars).executeUpdate();
            }
        });
        given().when().get("/products/7")
                .then().statusCode(200).body("reviewCount", equalTo(3)).body("rating", equalTo(4.33f));
        given().when().get("/products/8")
                .then().statusCode(200).body("reviewCount", equalTo(0)).body("$", not(hasKey("rating")));
        given().queryParam("size", 3).when().get("/products")
                .then().statusCode(200).body("[0]", not(hasKey("rating"))).body("[0]", not(hasKey("reviewCount")));
    }

    @Test
    void startupCreatesTheReviewIndex() {
        var count = em.createNativeQuery(
                        "SELECT count(*) FROM information_schema.indexes WHERE lower(index_name) = 'idx_review_product'")
                .getSingleResult();
        org.junit.jupiter.api.Assertions.assertTrue(((Number) count).longValue() > 0);
    }

    @Test
    void rejectsOversizedPage() {
        given().queryParam("size", 1000).when().get("/products").then().statusCode(400);
    }

    @Test
    void unknownProductIs404() {
        given().when().get("/products/999999").then().statusCode(404).body("error", equalTo("not_found"));
    }

    @Test
    void createUpdateDeletePublishesEveryChange() {
        long id = given().contentType(ContentType.JSON).body(body("T-100", "10.50"))
                .when().post("/products")
                .then().statusCode(201).body("name", equalTo("Test Phone"))
                .extract().jsonPath().getLong("id");
        verify(publisher).upserted(argThat(p -> p.id == id));

        given().contentType(ContentType.JSON).body(body("T-100", "12.00"))
                .when().put("/products/" + id)
                .then().statusCode(200).body("price", equalTo(12.0f));

        given().when().delete("/products/" + id).then().statusCode(204);
        verify(publisher).deleted(id);
        given().when().get("/products/" + id).then().statusCode(404);
    }

    @Test
    void invalidInputIsRejectedAndNotPublished() {
        given().contentType(ContentType.JSON).body(body("", "-1"))
                .when().post("/products")
                .then().statusCode(400);
        verify(publisher, never()).upserted(any());
    }

    @Test
    void healthEndpointsAreUp() {
        given().when().get("/q/health/live").then().statusCode(200);
        given().when().get("/q/health/ready").then().statusCode(200);
    }
}
