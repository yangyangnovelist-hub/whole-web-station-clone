import LocalizedClientLink from "@modules/common/components/localized-client-link"
import { convertToLocale } from "@lib/util/money"
import { HttpTypes } from "@medusajs/types"

type OverviewProps = {
  customer: HttpTypes.StoreCustomer | null
  orders: HttpTypes.StoreOrder[] | null
}

const Overview = ({ customer, orders }: OverviewProps) => {
  const recentOrders = orders?.slice(0, 4) || []
  const profileCompletion = getProfileCompletion(customer)
  const addressCount = customer?.addresses?.length || 0

  return (
    <div className="space-y-6" data-testid="overview-page-wrapper">
      <section className="rounded-[30px] border border-[#dce8da] bg-[radial-gradient(circle_at_top_left,_rgba(145,216,170,0.16),_transparent_42%),linear-gradient(180deg,_rgba(255,255,255,0.96)_0%,_rgba(247,251,244,0.98)_100%)] p-6 small:p-8">
        <div className="flex flex-col gap-6 lg:flex-row lg:items-end lg:justify-between">
          <div className="max-w-2xl">
            <p className="text-[11px] font-semibold uppercase tracking-[0.32em] text-[#3ea26f]">
              Account overview
            </p>
            <h1
              className="mt-3 font-serif text-[2.6rem] leading-[0.95] text-[#11263a] small:text-[3.3rem]"
              data-testid="welcome-message"
              data-value={customer?.first_name}
            >
              Hello {customer?.first_name || "there"}.
            </h1>
            <p className="mt-3 text-base leading-7 text-[#5e6f7f]">
              Your storefront account is connected to PackOasis quoting,
              production follow-up, and order tracking.
            </p>
          </div>
          <div className="rounded-[24px] border border-[#d7e5d6] bg-white/80 px-5 py-4">
            <p className="text-[11px] font-semibold uppercase tracking-[0.28em] text-[#8b9aa6]">
              Signed in as
            </p>
            <p
              className="mt-2 text-sm font-semibold text-[#17303b]"
              data-testid="customer-email"
              data-value={customer?.email}
            >
              {customer?.email}
            </p>
          </div>
        </div>
      </section>

      <section className="grid gap-4 lg:grid-cols-3">
        <MetricCard
          label="Profile completion"
          value={`${profileCompletion}%`}
          helper="Email, name, phone, and billing defaults"
          dataTestId="customer-profile-completion"
        />
        <MetricCard
          label="Saved addresses"
          value={`${addressCount}`}
          helper="Billing and shipping destinations stored"
          dataTestId="addresses-count"
        />
        <MetricCard
          label="Recent orders"
          value={`${orders?.length || 0}`}
          helper="Historical transactions connected to your account"
        />
      </section>

      <section className="rounded-[30px] border border-[#dce8da] bg-white/95 p-6 small:p-8">
        <div className="flex flex-col gap-4 border-b border-[#e4ece2] pb-5 small:flex-row small:items-end small:justify-between">
          <div>
            <p className="text-[11px] font-semibold uppercase tracking-[0.3em] text-[#3ea26f]">
              Recent orders
            </p>
            <h2 className="mt-3 font-serif text-[2rem] leading-tight text-[#11263a]">
              Keep production moving.
            </h2>
          </div>
          <div className="flex flex-wrap gap-3">
            <LocalizedClientLink
              href="/contact-us.html"
              className="inline-flex items-center rounded-full border border-[#ceddcb] px-5 py-2.5 text-sm font-semibold text-[#17303b] transition hover:border-[#8cc9a8] hover:bg-[#f8fcf6]"
            >
              Start another quote
            </LocalizedClientLink>
            <LocalizedClientLink
              href="/store"
              className="inline-flex items-center rounded-full bg-[#13273b] px-5 py-2.5 text-sm font-semibold text-white transition hover:bg-[#1b3550]"
            >
              Browse catalog
            </LocalizedClientLink>
          </div>
        </div>

        {recentOrders.length ? (
          <ul className="mt-6 space-y-4" data-testid="orders-wrapper">
            {recentOrders.map((order) => (
              <li
                key={order.id}
                className="rounded-[26px] border border-[#e4ece2] bg-[#fbfdf9] p-5 transition hover:border-[#a5d5b3] hover:shadow-[0_20px_50px_rgba(16,42,54,0.06)]"
                data-testid="order-wrapper"
                data-value={order.id}
              >
                <LocalizedClientLink
                  href={`/account/orders/details/${order.id}`}
                  className="grid gap-4 lg:grid-cols-[1.2fr_1fr_1fr_auto] lg:items-center"
                >
                  <div>
                    <p className="text-[11px] font-semibold uppercase tracking-[0.28em] text-[#8b9aa6]">
                      Date placed
                    </p>
                    <p
                      className="mt-2 text-sm font-semibold text-[#17303b]"
                      data-testid="order-created-date"
                    >
                      {new Date(order.created_at).toDateString()}
                    </p>
                  </div>
                  <div>
                    <p className="text-[11px] font-semibold uppercase tracking-[0.28em] text-[#8b9aa6]">
                      Order number
                    </p>
                    <p
                      className="mt-2 text-sm font-semibold text-[#17303b]"
                      data-testid="order-id"
                      data-value={order.display_id}
                    >
                      #{order.display_id}
                    </p>
                  </div>
                  <div>
                    <p className="text-[11px] font-semibold uppercase tracking-[0.28em] text-[#8b9aa6]">
                      Total
                    </p>
                    <p className="mt-2 text-sm font-semibold text-[#17303b]">
                      {convertToLocale({
                        amount: order.total,
                        currency_code: order.currency_code,
                      })}
                    </p>
                  </div>
                  <div className="text-sm font-semibold text-[#3ea26f]">
                    View details
                  </div>
                </LocalizedClientLink>
              </li>
            ))}
          </ul>
        ) : (
          <div className="mt-6 rounded-[26px] border border-dashed border-[#d7e5d6] bg-[#f8fcf6] px-6 py-8">
            <p
              className="text-base leading-7 text-[#5e6f7f]"
              data-testid="no-orders-message"
            >
              No recent orders yet. Once production orders are created from your
              quotes, they will appear here with status and totals.
            </p>
          </div>
        )}
      </section>
    </div>
  )
}

const MetricCard = ({
  label,
  value,
  helper,
  dataTestId,
}: {
  label: string
  value: string
  helper: string
  dataTestId?: string
}) => {
  return (
    <div className="rounded-[28px] border border-[#dce8da] bg-white/95 p-6 shadow-[0_20px_50px_rgba(16,42,54,0.05)]">
      <p className="text-[11px] font-semibold uppercase tracking-[0.28em] text-[#8b9aa6]">
        {label}
      </p>
      <p
        className="mt-4 font-serif text-[2.5rem] leading-none text-[#11263a]"
        data-testid={dataTestId}
        data-value={value.replace("%", "")}
      >
        {value}
      </p>
      <p className="mt-3 text-sm leading-6 text-[#5e6f7f]">{helper}</p>
    </div>
  )
}

const getProfileCompletion = (customer: HttpTypes.StoreCustomer | null) => {
  let count = 0

  if (!customer) {
    return 0
  }

  if (customer.email) {
    count++
  }

  if (customer.first_name && customer.last_name) {
    count++
  }

  if (customer.phone) {
    count++
  }

  const billingAddress = customer.addresses?.find(
    (addr) => addr.is_default_billing
  )

  if (billingAddress) {
    count++
  }

  return (count / 4) * 100
}

export default Overview
