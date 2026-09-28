"use client"

import { usePathname } from "next/navigation"
import { useEffect } from "react"

type PackOasisWindow = Window & {
  PackOasisQuote?: { onRoute?: () => void }
}

/**
 * The instant quote widget is loaded once in the root layout, so it never sees
 * App Router client-side navigations on its own. Tell it about each new route
 * so it can re-check where it is (e.g. hide the button during checkout).
 */
const PackOasisRouteSync = () => {
  const pathname = usePathname()

  useEffect(() => {
    ;(window as PackOasisWindow).PackOasisQuote?.onRoute?.()
  }, [pathname])

  return null
}

export default PackOasisRouteSync
