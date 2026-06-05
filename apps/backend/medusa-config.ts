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
        providers: [
          {
            resolve: "@medusajs/analytics-posthog",
            id: "posthog",
            options: {
              apiKey: process.env.POSTHOG_API_KEY || "phc_dummy",
              apiUrl: process.env.POSTHOG_API_URL || "https://app.posthog.com",
            },
          },
        ],
      },
    },
    {
      resolve: "./src/modules/rfq",
    },
    {
      resolve: "./src/modules/project-draft",
    },
  ],
})
