package dev.incidentlens.catalog;

import static io.restassured.RestAssured.given;
import static org.hamcrest.Matchers.equalTo;
import static org.hamcrest.Matchers.everyItem;
import static org.hamcrest.Matchers.hasSize;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.argThat;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;

import io.quarkus.test.InjectMock;
import io.quarkus.test.junit.QuarkusTest;
import io.restassured.http.ContentType;
import org.junit.jupiter.api.Test;

@QuarkusTest
class ProductResourceTest {

    @InjectMock
    ChangePublisher publisher;

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
