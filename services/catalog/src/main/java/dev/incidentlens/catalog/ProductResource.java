package dev.incidentlens.catalog;

import jakarta.validation.Valid;
import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.ws.rs.Consumes;
import jakarta.ws.rs.DELETE;
import jakarta.ws.rs.DefaultValue;
import jakarta.ws.rs.GET;
import jakarta.ws.rs.POST;
import jakarta.ws.rs.PUT;
import jakarta.ws.rs.Path;
import jakarta.ws.rs.PathParam;
import jakarta.ws.rs.Produces;
import jakarta.ws.rs.QueryParam;
import jakarta.ws.rs.core.MediaType;
import jakarta.ws.rs.core.Response;
import java.net.URI;
import java.util.List;
import java.util.Map;

@Path("/products")
@Produces(MediaType.APPLICATION_JSON)
@Consumes(MediaType.APPLICATION_JSON)
public class ProductResource {

    private final ProductService service;
    private final ChangePublisher publisher;

    public ProductResource(ProductService service, ChangePublisher publisher) {
        this.service = service;
        this.publisher = publisher;
    }

    @GET
    public List<Product> list(
            @QueryParam("category") String category,
            @QueryParam("page") @DefaultValue("0") @Min(0) int page,
            @QueryParam("size") @DefaultValue("20") @Min(1) @Max(100) int size) {
        return service.list(category, page, size);
    }

    @GET
    @Path("/{id}")
    public Response get(@PathParam("id") long id) {
        return service.get(id)
                .map(p -> Response.ok(p).build())
                .orElseGet(ProductResource::notFound);
    }

    // Writes go through ProductService so the transaction has committed
    // before the change event is published.
    @POST
    public Response create(@Valid ProductInput input) {
        var product = service.create(input);
        publisher.upserted(product);
        return Response.created(URI.create("/products/" + product.id)).entity(product).build();
    }

    @PUT
    @Path("/{id}")
    public Response update(@PathParam("id") long id, @Valid ProductInput input) {
        return service.update(id, input)
                .map(p -> {
                    publisher.upserted(p);
                    return Response.ok(p).build();
                })
                .orElseGet(ProductResource::notFound);
    }

    @DELETE
    @Path("/{id}")
    public Response delete(@PathParam("id") long id) {
        if (!service.delete(id)) {
            return notFound();
        }
        publisher.deleted(id);
        return Response.noContent().build();
    }

    private static Response notFound() {
        return Response.status(Response.Status.NOT_FOUND).entity(Map.of("error", "not_found")).build();
    }
}
