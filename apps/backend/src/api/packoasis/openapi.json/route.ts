import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { DEFAULT_PRICING } from "../../../lib/instant-quote/catalog"
import { packoasisConfig } from "../../../lib/packoasis-config"

/** OpenAPI description of the keyless quote API, for AI agents / GPT actions. */
export async function GET(req: MedusaRequest, res: MedusaResponse) {
  res.setHeader("Access-Control-Allow-Origin", "*")
  res.setHeader("Cache-Control", "public, max-age=3600")
  res.json({
    openapi: "3.1.0",
    info: {
      title: "PackOasis Instant Quote API",
      version: DEFAULT_PRICING.version,
      description:
        "Instant prices for custom printed packaging (mailer boxes, folding cartons, rigid boxes, pouches, bags, labels and more). Prices are in USD and include standard freight to the contiguous US. Each quote returns an order_url the buyer can open to order in a few clicks.",
    },
    servers: [{ url: packoasisConfig.backendUrl() }],
    paths: {
      "/packoasis/catalog": {
        get: {
          operationId: "getPackagingCatalog",
          summary:
            "List packaging product types, options, minimum order quantities and starting prices",
          responses: { "200": { description: "Catalog" } },
        },
      },
      "/packoasis/quote": {
        get: {
          operationId: "getInstantPackagingQuote",
          summary: "Get an instant price for custom packaging",
          parameters: [
            {
              name: "product_type",
              in: "query",
              required: true,
              schema: {
                type: "string",
                enum: Object.keys(DEFAULT_PRICING.product_types),
              },
            },
            {
              name: "dimensions",
              in: "query",
              description:
                "Dimensions separated by x, in the order given by the catalog (e.g. 10x8x4 for L x W x H)",
              schema: { type: "string" },
            },
            {
              name: "unit",
              in: "query",
              schema: { type: "string", enum: ["in", "cm", "mm"] },
            },
            { name: "quantity", in: "query", schema: { type: "integer" } },
            { name: "material", in: "query", schema: { type: "string" } },
            {
              name: "print",
              in: "query",
              schema: {
                type: "string",
                enum: ["none", "one_color", "cmyk_outside", "cmyk_both"],
              },
            },
            {
              name: "finishes",
              in: "query",
              description: "Comma separated finish ids",
              schema: { type: "string" },
            },
            { name: "addons", in: "query", schema: { type: "string" } },
            { name: "rush", in: "query", schema: { type: "boolean" } },
          ],
          responses: {
            "200": {
              description:
                "Quote with unit price, total, lead time, volume tiers and order_url",
            },
            "400": { description: "Invalid specs" },
          },
        },
      },
    },
  })
}
