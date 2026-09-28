import { ExecArgs } from "@medusajs/framework/types"
import { ContainerRegistrationKeys, Modules } from "@medusajs/framework/utils"
import {
  createRegionsWorkflow,
  createShippingOptionsWorkflow,
  createShippingProfilesWorkflow,
  createStockLocationsWorkflow,
  createTaxRegionsWorkflow,
  linkSalesChannelsToStockLocationWorkflow,
  updateRegionsWorkflow,
} from "@medusajs/medusa/core-flows"
import { loadPricingConfig } from "../lib/instant-quote/engine"

const FREIGHT_OPTION_NAME = "Freight included in your quote"
const ZONE_NAME = "Instant quote freight"
const COUNTRY_NAMES: Record<string, string> = {
  us: "United States",
  ca: "Canada",
  gb: "United Kingdom",
  au: "Australia",
}

/**
 * Idempotent setup so instant quotes can be ordered end to end:
 *   npx medusa exec ./src/scripts/setup-instant-quote.ts
 *
 * - a region per INSTANT_QUOTE_COUNTRIES country in the pricing currency
 *   (manual payment, plus Stripe when STRIPE_API_KEY is set)
 * - a tax region per country
 * - a stock location linked to the storefront sales channel with the manual
 *   fulfillment provider
 * - a service zone for those countries and a $0 "Freight included in your
 *   quote" shipping option (instant quote prices already include freight)
 */
export default async function setupInstantQuote({ container }: ExecArgs) {
  const logger = container.resolve(ContainerRegistrationKeys.LOGGER)
  const query = container.resolve(ContainerRegistrationKeys.QUERY)
  const link = container.resolve(ContainerRegistrationKeys.LINK)
  const fulfillmentService = container.resolve(Modules.FULFILLMENT)
  const taxService = container.resolve(Modules.TAX)

  const currency = loadPricingConfig().currency_code
  const countries = (process.env.INSTANT_QUOTE_COUNTRIES || "us")
    .split(",")
    .map((code) => code.trim().toLowerCase())
    .filter((code) => /^[a-z]{2}$/.test(code))
  const paymentProviders = [
    "pp_system_default",
    ...(process.env.STRIPE_API_KEY ? ["pp_stripe_stripe"] : []),
  ]

  // 1. Regions
  const { data: regions } = await query.graph({
    entity: "region",
    fields: [
      "id",
      "name",
      "currency_code",
      "countries.iso_2",
      "payment_providers.id",
    ],
  })
  for (const countryCode of countries) {
    const region = regions.find((candidate: any) =>
      candidate.countries?.some(
        (country: any) => country?.iso_2 === countryCode
      )
    )
    if (!region) {
      await createRegionsWorkflow(container).run({
        input: {
          regions: [
            {
              name: COUNTRY_NAMES[countryCode] ?? countryCode.toUpperCase(),
              currency_code: currency,
              countries: [countryCode],
              payment_providers: paymentProviders,
            },
          ],
        },
      })
      logger.info(`[setup] created region for ${countryCode} (${currency})`)
      continue
    }
    if (region.currency_code !== currency) {
      logger.warn(
        `[setup] ${countryCode} is in region "${region.name}" (${region.currency_code}); instant quotes are priced in ${currency}, so orders for ${countryCode} go to manual review.`
      )
      continue
    }
    const existing = (region.payment_providers ?? []).map(
      (provider: any) => provider?.id
    )
    const missing = paymentProviders.filter((id) => !existing.includes(id))
    if (missing.length) {
      await updateRegionsWorkflow(container).run({
        input: {
          selector: { id: region.id },
          update: { payment_providers: [...existing, ...missing] },
        },
      })
      logger.info(
        `[setup] enabled ${missing.join(", ")} on region "${region.name}"`
      )
    }
  }

  // 2. Tax regions
  const taxRegions = await taxService.listTaxRegions({
    country_code: countries,
  })
  const missingTax = countries.filter(
    (code) =>
      !taxRegions.some(
        (taxRegion) => taxRegion.country_code === code && !taxRegion.parent_id
      )
  )
  if (missingTax.length) {
    await createTaxRegionsWorkflow(container).run({
      input: missingTax.map((country_code) => ({
        country_code,
        provider_id: "tp_system",
      })),
    })
    logger.info(`[setup] created tax regions for ${missingTax.join(", ")}`)
  }

  // 3. Stock location for the storefront sales channel
  const {
    data: [store],
  } = await query.graph({
    entity: "store",
    fields: ["id", "default_sales_channel_id"],
  })
  const salesChannelId =
    process.env.INSTANT_QUOTE_SALES_CHANNEL_ID ||
    store?.default_sales_channel_id
  if (!salesChannelId) {
    throw new Error(
      "No sales channel found: set INSTANT_QUOTE_SALES_CHANNEL_ID"
    )
  }

  const channelFields = [
    "id",
    "name",
    "stock_locations.id",
    "stock_locations.name",
    "stock_locations.fulfillment_providers.id",
    "stock_locations.fulfillment_sets.id",
    "stock_locations.fulfillment_sets.type",
    "stock_locations.fulfillment_sets.service_zones.id",
    "stock_locations.fulfillment_sets.service_zones.name",
    "stock_locations.fulfillment_sets.service_zones.geo_zones.country_code",
  ]
  const loadChannel = async (): Promise<any> =>
    (
      await query.graph({
        entity: "sales_channel",
        fields: channelFields,
        filters: { id: salesChannelId },
      })
    ).data[0]

  let channel: any = await loadChannel()
  if (!channel) {
    throw new Error(`Sales channel ${salesChannelId} not found`)
  }

  let location: any = channel.stock_locations?.[0]
  if (!location) {
    const { result } = await createStockLocationsWorkflow(container).run({
      input: {
        locations: [
          {
            name: "PackOasis Factory",
            address: {
              address_1: "",
              city: "",
              country_code: (
                process.env.INSTANT_QUOTE_WAREHOUSE_COUNTRY || "us"
              ).toUpperCase(),
            },
          },
        ],
      },
    })
    await linkSalesChannelsToStockLocationWorkflow(container).run({
      input: { id: result[0].id, add: [salesChannelId] },
    })
    logger.info(`[setup] created stock location "PackOasis Factory"`)
    channel = await loadChannel()
    location = channel.stock_locations[0]
  }

  if (
    !location.fulfillment_providers?.some(
      (provider: any) => provider?.id === "manual_manual"
    )
  ) {
    await link.create({
      [Modules.STOCK_LOCATION]: { stock_location_id: location.id },
      [Modules.FULFILLMENT]: { fulfillment_provider_id: "manual_manual" },
    })
    logger.info(`[setup] enabled manual fulfillment on "${location.name}"`)
  }

  // 4. Fulfillment set + service zone for the instant quote countries
  let fulfillmentSet: any = location.fulfillment_sets?.find(
    (set: any) => set?.type === "shipping"
  )
  if (!fulfillmentSet) {
    fulfillmentSet = await fulfillmentService.createFulfillmentSets({
      name: `${location.name} freight`,
      type: "shipping",
    })
    await link.create({
      [Modules.STOCK_LOCATION]: { stock_location_id: location.id },
      [Modules.FULFILLMENT]: { fulfillment_set_id: fulfillmentSet.id },
    })
    logger.info(`[setup] created fulfillment set "${fulfillmentSet.name}"`)
  }

  let zone: any = fulfillmentSet.service_zones?.find(
    (candidate: any) => candidate?.name === ZONE_NAME
  )
  if (!zone) {
    zone = await fulfillmentService.createServiceZones({
      name: ZONE_NAME,
      fulfillment_set_id: fulfillmentSet.id,
      geo_zones: countries.map((country_code) => ({
        type: "country" as const,
        country_code,
      })),
    })
    logger.info(
      `[setup] created service zone "${ZONE_NAME}" for ${countries.join(", ")}`
    )
  } else {
    const covered = (zone.geo_zones ?? []).map((geo: any) => geo?.country_code)
    const uncovered = countries.filter((code) => !covered.includes(code))
    if (uncovered.length) {
      await fulfillmentService.createGeoZones(
        uncovered.map((country_code) => ({
          service_zone_id: zone.id,
          type: "country" as const,
          country_code,
        }))
      )
      logger.info(`[setup] added ${uncovered.join(", ")} to "${ZONE_NAME}"`)
    }
  }

  // 5. $0 freight-included shipping option
  const options = await fulfillmentService.listShippingOptions({
    service_zone_id: zone.id,
  } as any)
  if (!options.some((option) => option.name === FREIGHT_OPTION_NAME)) {
    let [profile] = await fulfillmentService.listShippingProfiles({
      type: "default",
    })
    if (!profile) {
      const { result } = await createShippingProfilesWorkflow(container).run({
        input: {
          data: [{ name: "Default Shipping Profile", type: "default" }],
        },
      })
      profile = result[0]
    }
    await createShippingOptionsWorkflow(container).run({
      input: [
        {
          name: FREIGHT_OPTION_NAME,
          price_type: "flat",
          provider_id: "manual_manual",
          service_zone_id: zone.id,
          shipping_profile_id: profile.id,
          type: {
            label: "Freight included",
            description:
              "Standard freight is already included in your PackOasis quote.",
            code: "freight-included",
          },
          prices: [{ currency_code: currency, amount: 0 }],
          rules: [
            { attribute: "enabled_in_store", value: "true", operator: "eq" },
            { attribute: "is_return", value: "false", operator: "eq" },
          ],
        },
      ],
    })
    logger.info(`[setup] created shipping option "${FREIGHT_OPTION_NAME}"`)
  }

  logger.info(
    `[setup] instant quote ready for ${countries.join(", ")} on sales channel "${channel.name}"`
  )
}
