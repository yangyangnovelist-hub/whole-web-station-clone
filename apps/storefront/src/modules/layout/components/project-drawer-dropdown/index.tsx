"use client"

import {
  Popover,
  PopoverButton,
  PopoverPanel,
  Transition,
} from "@headlessui/react"
import { Spinner, Trash } from "@medusajs/icons"
import LocalizedClientLink from "@modules/common/components/localized-client-link"
import { Fragment, useEffect, useMemo, useState, useTransition } from "react"
import { usePathname, useRouter } from "next/navigation"

import { removeProjectDrawerItem } from "@modules/layout/actions"

type DraftItem = {
  id: string
  title: string
  quantity?: number
  product_handle?: string
  sku?: string
  url?: string
  image?: string
  notes?: string
  metadata?: Record<string, unknown>
}

type Draft = {
  id: string
  items?: DraftItem[]
} | null

const truncate = (value?: string | null, limit = 88) => {
  if (!value) {
    return ""
  }

  if (value.length <= limit) {
    return value
  }

  return `${value.slice(0, limit).trim()}...`
}

export default function ProjectDrawerDropdown({
  draft,
}: {
  draft?: Draft
}) {
  const router = useRouter()
  const pathname = usePathname()

  const initialItems = useMemo(() => draft?.items || [], [draft?.id, draft?.items])
  const [items, setItems] = useState(initialItems)
  const [activeTimer, setActiveTimer] = useState<NodeJS.Timeout | undefined>()
  const [isOpen, setIsOpen] = useState(false)
  const [isPending, startTransition] = useTransition()
  const [removingId, setRemovingId] = useState<string | null>(null)
  const projectHref = draft?.id
    ? `/contact-us.html?draft_id=${encodeURIComponent(draft.id)}`
    : "/contact-us.html"

  useEffect(() => {
    setItems(initialItems)
  }, [draft?.id, initialItems])

  const totalItems = useMemo(
    () => items.reduce((acc, item) => acc + (item.quantity || 1), 0),
    [items]
  )

  const open = () => setIsOpen(true)
  const close = () => setIsOpen(false)

  const openAndCancel = () => {
    if (activeTimer) {
      clearTimeout(activeTimer)
    }

    open()
  }

  const timedOpen = () => {
    open()

    const timer = setTimeout(close, 4500)
    setActiveTimer(timer)
  }

  useEffect(() => {
    return () => {
      if (activeTimer) {
        clearTimeout(activeTimer)
      }
    }
  }, [activeTimer])

  useEffect(() => {
    if (pathname.includes("/contact-us")) {
      close()
    }
  }, [pathname])

  const handleRemove = (itemId: string) => {
    const previousItems = items

    setRemovingId(itemId)
    setItems((current) => current.filter((item) => item.id !== itemId))

    startTransition(async () => {
      try {
        await removeProjectDrawerItem(itemId)
        timedOpen()
      } catch {
        setItems(previousItems)
      } finally {
        setRemovingId(null)
        router.refresh()
      }
    })
  }

  return (
    <div
      className="h-full z-50"
      onMouseEnter={openAndCancel}
      onMouseLeave={close}
    >
      <Popover className="relative h-full">
        <PopoverButton className="h-full">
          <LocalizedClientLink
            href={projectHref}
            className="hover:text-ui-fg-base"
            data-testid="nav-projects-link"
          >{`Projects (${totalItems})`}</LocalizedClientLink>
        </PopoverButton>
        <Transition
          show={isOpen}
          as={Fragment}
          enter="transition ease-out duration-200"
          enterFrom="opacity-0 translate-y-1"
          enterTo="opacity-100 translate-y-0"
          leave="transition ease-in duration-150"
          leaveFrom="opacity-100 translate-y-0"
          leaveTo="opacity-0 translate-y-1"
        >
          <PopoverPanel
            static
            className="hidden small:block absolute top-[calc(100%+1px)] right-0 bg-white border-x border-b border-gray-200 w-[430px] text-ui-fg-base"
            data-testid="nav-project-drawer-dropdown"
          >
            <div className="border-b border-gray-100 px-5 py-4">
              <p className="text-[11px] font-semibold uppercase tracking-[0.28em] text-emerald-600">
                PackOasis Project Drawer
              </p>
              <div className="mt-2 flex items-center justify-between gap-4">
                <h3 className="text-large-semi">My Projects</h3>
                <span className="text-small-regular text-ui-fg-subtle">
                  {totalItems} item{totalItems === 1 ? "" : "s"}
                </span>
              </div>
            </div>

            {items.length ? (
              <>
                <div className="max-h-[420px] overflow-y-auto px-4 py-4 no-scrollbar">
                  <div className="grid grid-cols-1 gap-y-4">
                    {items.map((item) => (
                      <div
                        key={item.id}
                        className="grid grid-cols-[80px_1fr] gap-4 rounded-2xl border border-slate-100 p-3"
                      >
                        <div className="h-20 overflow-hidden rounded-xl bg-slate-100">
                          {item.image ? (
                            <img
                              src={item.image}
                              alt={item.title}
                              className="h-full w-full object-cover"
                            />
                          ) : (
                            <div className="flex h-full items-center justify-center bg-slate-900 text-xs font-semibold uppercase tracking-[0.22em] text-white">
                              RFQ
                            </div>
                          )}
                        </div>

                        <div className="min-w-0">
                          <div className="flex items-start justify-between gap-3">
                            <div className="min-w-0">
                              <p className="truncate text-base font-semibold text-slate-900">
                                {item.url ? (
                                  <LocalizedClientLink
                                    href={item.url.replace(/^\/[a-z]{2}/, "")}
                                    className="hover:text-slate-700"
                                  >
                                    {item.title}
                                  </LocalizedClientLink>
                                ) : (
                                  item.title
                                )}
                              </p>
                              <p className="mt-1 text-sm text-slate-500">
                                Qty: {item.quantity || 1}
                                {item.sku ? ` · SKU: ${item.sku}` : ""}
                              </p>
                            </div>

                            <button
                              type="button"
                              onClick={() => handleRemove(item.id)}
                              className="flex items-center gap-1 text-xs text-slate-500 transition hover:text-slate-900"
                              disabled={isPending && removingId === item.id}
                            >
                              {isPending && removingId === item.id ? (
                                <Spinner className="animate-spin" />
                              ) : (
                                <Trash />
                              )}
                              <span>Remove</span>
                            </button>
                          </div>

                          {item.notes && (
                            <p className="mt-3 text-sm leading-6 text-slate-600">
                              {truncate(item.notes)}
                            </p>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>

                <div className="border-t border-gray-100 p-4">
                  <div className="flex flex-col gap-3">
                    <LocalizedClientLink
                      href={projectHref}
                      className="inline-flex w-full items-center justify-center rounded-full bg-slate-900 px-4 py-3 text-sm font-semibold text-white transition hover:bg-slate-700"
                    >
                      Review and submit RFQ
                    </LocalizedClientLink>
                    <LocalizedClientLink
                      href="/store"
                      className="inline-flex w-full items-center justify-center rounded-full border border-slate-300 px-4 py-3 text-sm font-semibold text-slate-700 transition hover:border-slate-900 hover:text-slate-900"
                    >
                      Continue browsing
                    </LocalizedClientLink>
                  </div>
                </div>
              </>
            ) : (
              <div className="px-6 py-14 text-center">
                <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-slate-900 text-sm font-semibold text-white">
                  0
                </div>
                <p className="mt-4 text-base font-semibold text-slate-900">
                  Your project drawer is empty.
                </p>
                <p className="mt-2 text-sm leading-6 text-slate-500">
                  Add products from detail pages, then submit a unified RFQ from
                  the PackOasis intake flow.
                </p>
                <div className="mt-5">
                  <LocalizedClientLink
                    href="/store"
                    className="inline-flex items-center justify-center rounded-full bg-slate-900 px-5 py-3 text-sm font-semibold text-white transition hover:bg-slate-700"
                  >
                    Explore products
                  </LocalizedClientLink>
                </div>
              </div>
            )}
          </PopoverPanel>
        </Transition>
      </Popover>
    </div>
  )
}
