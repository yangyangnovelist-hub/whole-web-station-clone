import routeInventory from "../../../../../docs/pakfactory-route-inventory.json"

type RouteRecord = {
  route: string
  relativePath: string
}

const htmlRoutes = (routeInventory.routes as RouteRecord[]).filter(
  (route) => typeof route.route === "string" && typeof route.relativePath === "string"
)

const assetPrefixes = [
  "/media.pakfactory.com",
  "/static.pakfactory.com",
  "/media.packoasis.com",
  "/static.packoasis.com",
  "/primary",
]

const routeToAssetPath = new Map<string, string>()

for (const route of htmlRoutes) {
  const publicPath = `/${route.relativePath}`
  routeToAssetPath.set(route.route, publicPath)

  if (route.route.endsWith(".html")) {
    routeToAssetPath.set(route.route.slice(0, -5), publicPath)
  }
}

routeToAssetPath.set("/", "/index.html")

export function getMirrorAliasRedirect(_pathname: string) {
  return null
}

export function getMirrorAssetRewrite(pathname: string) {
  if (!pathname) {
    return null
  }

  const normalizedPath =
    pathname !== "/" && pathname.endsWith("/") ? pathname.slice(0, -1) : pathname

  if (assetPrefixes.some((prefix) => normalizedPath === prefix || normalizedPath.startsWith(`${prefix}/`))) {
    return normalizedPath
  }

  return routeToAssetPath.get(normalizedPath) || null
}
