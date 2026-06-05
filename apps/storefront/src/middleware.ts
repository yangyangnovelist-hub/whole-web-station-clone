import { HttpTypes } from "@medusajs/types"
import { NextRequest, NextResponse } from "next/server"
import { getMirrorAliasRedirect, getMirrorAssetRewrite } from "@lib/mirror/routes"

const BACKEND_URL = process.env.MEDUSA_BACKEND_URL
const PUBLISHABLE_API_KEY = process.env.NEXT_PUBLIC_MEDUSA_PUBLISHABLE_KEY
const DEFAULT_REGION = process.env.NEXT_PUBLIC_DEFAULT_REGION || "us"
const REGION_FETCH_ATTEMPTS = 3
const REGION_FETCH_TIMEOUT_MS = 8000

const regionMapCache = {
  regionMap: new Map<string, HttpTypes.StoreRegion>(),
  regionMapUpdated: Date.now(),
}

async function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function shouldRetryRegionFetch(error: unknown) {
  if (
    error instanceof TypeError ||
    (error instanceof Error &&
      /ECONNRESET|fetch failed|ETIMEDOUT|socket hang up|EAI_AGAIN|Network connection lost|timed out|abort/i.test(
        error.message
      ))
  ) {
    return true
  }

  if (
    typeof error === "object" &&
    error !== null &&
    "retryable" in error &&
    (error as { retryable?: unknown }).retryable === true
  ) {
    return true
  }

  return false
}

async function fetchRegions(cacheId: string) {
  let lastError: unknown

  for (
    let attempt = 1;
    attempt <= REGION_FETCH_ATTEMPTS;
    attempt++
  ) {
    const controller = new AbortController()
    const timeout = setTimeout(
      () => controller.abort("Timed out while fetching regions"),
      REGION_FETCH_TIMEOUT_MS
    )

    try {
      const response = await fetch(`${BACKEND_URL}/store/regions`, {
        headers: {
          "x-publishable-api-key": PUBLISHABLE_API_KEY!,
        },
        next: {
          revalidate: 3600,
          tags: [`regions-${cacheId}`],
        },
        cache: "force-cache",
        signal: controller.signal,
      })

      const json = await response.json()

      if (!response.ok) {
        throw new Error(json.message)
      }

      return json.regions as HttpTypes.StoreRegion[]
    } catch (error) {
      lastError = error

      if (
        attempt === REGION_FETCH_ATTEMPTS ||
        !shouldRetryRegionFetch(error)
      ) {
        throw error
      }

      await sleep(250 * attempt)
    } finally {
      clearTimeout(timeout)
    }
  }

  throw lastError
}

async function getRegionMap(cacheId: string) {
  const { regionMap, regionMapUpdated } = regionMapCache

  if (!BACKEND_URL) {
    throw new Error(
      "Middleware.ts: Error fetching regions. Did you set up regions in your Medusa Admin and define a MEDUSA_BACKEND_URL environment variable? Note that the variable is no longer named NEXT_PUBLIC_MEDUSA_BACKEND_URL."
    )
  }

  if (
    !regionMap.keys().next().value ||
    regionMapUpdated < Date.now() - 3600 * 1000
  ) {
    // Middleware runs in the request path, so a transient backend blip should
    // not immediately fail the entire storefront entry request.
    const regions = await fetchRegions(cacheId)

    if (!regions?.length) {
      throw new Error(
        "No regions found. Please set up regions in your Medusa Admin."
      )
    }

    // Create a map of country codes to regions.
    regions.forEach((region: HttpTypes.StoreRegion) => {
      region.countries?.forEach((c) => {
        regionMapCache.regionMap.set(c.iso_2 ?? "", region)
      })
    })

    regionMapCache.regionMapUpdated = Date.now()
  }

  return regionMapCache.regionMap
}

/**
 * Fetches regions from Medusa and sets the region cookie.
 * @param request
 * @param response
 */
async function getCountryCode(
  request: NextRequest,
  regionMap: Map<string, HttpTypes.StoreRegion | number>
) {
  try {
    let countryCode

    const vercelCountryCode = request.headers
      .get("x-vercel-ip-country")
      ?.toLowerCase()

    const urlCountryCode = request.nextUrl.pathname.split("/")[1]?.toLowerCase()

    if (urlCountryCode && regionMap.has(urlCountryCode)) {
      countryCode = urlCountryCode
    } else if (vercelCountryCode && regionMap.has(vercelCountryCode)) {
      countryCode = vercelCountryCode
    } else if (regionMap.has(DEFAULT_REGION)) {
      countryCode = DEFAULT_REGION
    } else if (regionMap.keys().next().value) {
      countryCode = regionMap.keys().next().value
    }

    return countryCode
  } catch (error) {
    if (process.env.NODE_ENV === "development") {
      console.error(
        "Middleware.ts: Error getting the country code. Did you set up regions in your Medusa Admin and define a MEDUSA_BACKEND_URL environment variable? Note that the variable is no longer named NEXT_PUBLIC_MEDUSA_BACKEND_URL."
      )
    }
  }
}

/**
 * Middleware to handle region selection and onboarding status.
 */
export async function middleware(request: NextRequest) {
  const pathname = request.nextUrl.pathname
  const firstSegment = pathname.split("/")[1]?.toLowerCase() || ""
  const isCountryScopedRoute = /^[a-z]{2}$/.test(firstSegment)

  if (!isCountryScopedRoute) {
    const aliasRedirect = getMirrorAliasRedirect(pathname)

    if (aliasRedirect) {
      const redirectUrl = request.nextUrl.clone()
      redirectUrl.pathname = aliasRedirect
      return NextResponse.redirect(redirectUrl, 307)
    }

    const mirrorRewrite = getMirrorAssetRewrite(pathname)

    if (mirrorRewrite) {
      if (mirrorRewrite === pathname) {
        return NextResponse.next()
      }

      const rewriteUrl = request.nextUrl.clone()
      rewriteUrl.pathname = mirrorRewrite
      return NextResponse.rewrite(rewriteUrl)
    }
  }

  let redirectUrl = request.nextUrl.href

  let response = NextResponse.redirect(redirectUrl, 307)
  const requestHeaders = new Headers(request.headers)
  requestHeaders.set("x-packoasis-pathname", request.nextUrl.pathname)

  let cacheIdCookie = request.cookies.get("_medusa_cache_id")

  let cacheId = cacheIdCookie?.value || crypto.randomUUID()

  const regionMap = await getRegionMap(cacheId)

  const countryCode = regionMap && (await getCountryCode(request, regionMap))

  const urlHasCountryCode =
    countryCode && request.nextUrl.pathname.split("/")[1].includes(countryCode)

  // if one of the country codes is in the url and the cache id is set, return next
  if (urlHasCountryCode && cacheIdCookie) {
    return NextResponse.next({
      request: {
        headers: requestHeaders,
      },
    })
  }

  // if one of the country codes is in the url and the cache id is not set, set the cache id and redirect
  if (urlHasCountryCode && !cacheIdCookie) {
    response.cookies.set("_medusa_cache_id", cacheId, {
      maxAge: 60 * 60 * 24,
    })

    return response
  }

  // check if the url is a static asset
  if (request.nextUrl.pathname.includes(".")) {
    return NextResponse.next({
      request: {
        headers: requestHeaders,
      },
    })
  }

  const redirectPath =
    request.nextUrl.pathname === "/" ? "" : request.nextUrl.pathname

  const queryString = request.nextUrl.search ? request.nextUrl.search : ""

  // If no country code is set, we redirect to the relevant region.
  if (!urlHasCountryCode && countryCode) {
    redirectUrl = `${request.nextUrl.origin}/${countryCode}${redirectPath}${queryString}`
    response = NextResponse.redirect(`${redirectUrl}`, 307)
  } else if (!urlHasCountryCode && !countryCode) {
    // Handle case where no valid country code exists (empty regions)
    return new NextResponse(
      "No valid regions configured. Please set up regions with countries in your Medusa Admin.",
      { status: 500 }
    )
  }

  return response
}

export const config = {
  matcher: [
    "/((?!api|_next/static|_next/image|favicon.ico|images|assets|png|svg|jpg|jpeg|gif|webp).*)",
  ],
}
