import { ContainerRegistrationKeys } from "@medusajs/framework/utils"
import { loadEnv, defineConfig } from "@medusajs/framework/utils"

loadEnv(process.env.NODE_ENV || "development", process.cwd())

const storeCors =
  process.env.STORE_CORS ||
  "http://localhost:8000,http://127.0.0.1:8000,https://packoasis.com,https://www.packoasis.com"
const adminCors =
  process.env.ADMIN_CORS ||
  "http://localhost:7001,http://localhost:9000,http://127.0.0.1:7001,http://127.0.0.1:9000,https://admin.packoasis.com,https://packoasis.com"
const authCors =
  process.env.AUTH_CORS ||
  "http://localhost:7001,http://localhost:8000,http://localhost:9000,http://127.0.0.1:7001,http://127.0.0.1:8000,http://127.0.0.1:9000,https://packoasis.com,https://www.packoasis.com,https://admin.packoasis.com"

const authProviders: Array<{
  resolve: string
  id: string
  options?: {
    clientId: string
    clientSecret: string
    callbackUrl: string
  }
}> = [
  {
    resolve: "@medusajs/medusa/auth-emailpass",
    id: "emailpass",
  },
]

if (
  process.env.AUTH_GOOGLE_CLIENT_ID &&
  process.env.AUTH_GOOGLE_CLIENT_SECRET &&
  process.env.AUTH_GOOGLE_CALLBACK_URL
) {
  authProviders.push({
    resolve: "@medusajs/medusa/auth-google",
    id: "google",
    options: {
      clientId: process.env.AUTH_GOOGLE_CLIENT_ID,
      clientSecret: process.env.AUTH_GOOGLE_CLIENT_SECRET,
      callbackUrl: process.env.AUTH_GOOGLE_CALLBACK_URL,
    },
  })
}

// The PostHog provider reads `posthogEventsKey` / `posthogHost` and throws at
// boot when the key is missing, so only register it when a key is configured.
const posthogEventsKey =
  process.env.POSTHOG_EVENTS_API_KEY || process.env.POSTHOG_API_KEY
const analyticsProvider = posthogEventsKey
  ? {
      resolve: "@medusajs/analytics-posthog",
      id: "posthog",
      options: {
        posthogEventsKey,
        posthogHost:
          process.env.POSTHOG_HOST ||
          process.env.POSTHOG_API_URL ||
          "https://us.i.posthog.com",
      },
    }
  : {
      resolve: "@medusajs/medusa/analytics-local",
      id: "local",
    }

// Automation emails (quotes, follow-ups, order confirmations, sales alerts)
// go through Medusa's Notification module: Resend or SendGrid when a key is
// set, otherwise the local provider just logs them.
const emailFrom =
  process.env.PACKOASIS_EMAIL_FROM || "PackOasis <quotes@packoasis.com>"
const emailProvider = process.env.RESEND_API_KEY
  ? {
      resolve: "./src/modules/resend-notification",
      id: "resend",
      options: {
        channels: ["email"],
        api_key: process.env.RESEND_API_KEY,
        from: emailFrom,
      },
    }
  : process.env.SENDGRID_API_KEY
    ? {
        resolve: "@medusajs/medusa/notification-sendgrid",
        id: "sendgrid",
        options: {
          channels: ["email"],
          api_key: process.env.SENDGRID_API_KEY,
          from: emailFrom,
        },
      }
    : {
        resolve: "@medusajs/medusa/notification-local",
        id: "local",
        options: { channels: ["email"] },
      }

// Card payments at checkout. Without a key only the manual (system)
// provider exists, which places the order and invoices offline.
const paymentModules = process.env.STRIPE_API_KEY
  ? [
      {
        resolve: "@medusajs/medusa/payment",
        options: {
          providers: [
            {
              resolve: "@medusajs/medusa/payment-stripe",
              id: "stripe",
              options: {
                apiKey: process.env.STRIPE_API_KEY,
                webhookSecret: process.env.STRIPE_WEBHOOK_SECRET,
                capture: process.env.STRIPE_AUTO_CAPTURE === "true",
              },
            },
          ],
        },
      },
    ]
  : []

module.exports = defineConfig({
  projectConfig: {
    databaseUrl: process.env.DATABASE_URL,
    http: {
      storeCors,
      adminCors,
      authCors,
      authMethodsPerActor: {
        user: ["emailpass"],
        customer: authProviders.some((provider) => provider.id === "google")
          ? ["emailpass", "google"]
          : ["emailpass"],
      },
      jwtSecret: process.env.JWT_SECRET || "supersecret",
      cookieSecret: process.env.COOKIE_SECRET || "supersecret",
    },
  },
  admin: {
    path: "/app",
    backendUrl: process.env.MEDUSA_BACKEND_URL,
    disable: process.env.DISABLE_MEDUSA_ADMIN === "true",
  },
  modules: [
    {
      resolve: "@medusajs/medusa/auth",
      dependencies: ["cache", ContainerRegistrationKeys.LOGGER],
      options: {
        providers: authProviders,
      },
    },
    {
      resolve: "@medusajs/medusa/analytics",
      options: {
        providers: [analyticsProvider],
      },
    },
    {
      resolve: "./src/modules/rfq",
    },
    {
      resolve: "./src/modules/project-draft",
    },
    {
      resolve: "./src/modules/lead",
    },
    {
      resolve: "@medusajs/medusa/notification",
      options: {
        providers: [emailProvider],
      },
    },
    ...paymentModules,
  ],
})
