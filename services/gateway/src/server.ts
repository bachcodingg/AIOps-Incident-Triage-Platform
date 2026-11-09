// First import: patches the http module before Fastify loads it.
import { shutdownTelemetry } from "./telemetry.js";
import { buildApp } from "./app.js";
import { loadConfig } from "./config.js";

const config = loadConfig();
const app = await buildApp({ config });

for (const signal of ["SIGTERM", "SIGINT"] as const) {
  process.once(signal, async () => {
    app.log.info({ signal }, "shutting down");
    await app.close();
    await shutdownTelemetry();
    process.exit(0);
  });
}

await app.listen({ host: "0.0.0.0", port: config.port });
