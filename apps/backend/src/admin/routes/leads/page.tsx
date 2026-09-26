import { defineRouteConfig } from "@medusajs/admin-sdk"
import { SparklesSolid } from "@medusajs/icons"
import {
  Badge,
  Button,
  Container,
  Heading,
  Input,
  Select,
  Table,
  Text,
} from "@medusajs/ui"
import { useCallback, useEffect, useState } from "react"

type Rfq = {
  id: string
  created_at: string
  contact_name: string
  email: string
  company: string | null
  phone: string | null
  website: string | null
  title: string
  source: string
  status: string
  quantity: number
  quoted_total: number | null
  currency_code: string | null
  lead_score: number | null
  lead_grade: string | null
  order_id: string | null
  cart_id: string | null
  followup_count: number
  contact_opt_out: boolean
  notes: string | null
  quote_payload: any
  enrichment_payload: any
  quotes?: {
    id: string
    round: number
    price: number
    status: string
    notes: string | null
  }[]
}

type Detail = { rfq: Rfq; trail: any; events: any[] }

const NEXT_STATUS: Record<string, string[]> = {
  SUBMITTED: ["QUOTED", "CLOSED"],
  QUOTED: ["ACCEPTED", "CLOSED"],
  COUNTER: ["QUOTED", "CLOSED"],
  ACCEPTED: ["ORDERED"],
}

const gradeColor = (grade: string | null) =>
  grade === "A"
    ? "green"
    : grade === "B"
      ? "blue"
      : grade === "C"
        ? "orange"
        : "grey"

const statusColor = (status: string) =>
  status === "ORDERED"
    ? "green"
    : status === "CLOSED"
      ? "grey"
      : status === "QUOTED"
        ? "blue"
        : "orange"

const money = (amount: number | null, currency: string | null) =>
  amount == null
    ? "-"
    : new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: (currency || "usd").toUpperCase(),
      }).format(amount)

async function getJson(path: string, init?: RequestInit) {
  const res = await fetch(path, {
    credentials: "include",
    headers: { "content-type": "application/json" },
    ...init,
  })
  if (!res.ok) {
    throw new Error(`${res.status} ${await res.text()}`)
  }
  return res.json()
}

const LeadsPage = () => {
  const [rfqs, setRfqs] = useState<Rfq[]>([])
  const [count, setCount] = useState(0)
  const [q, setQ] = useState("")
  const [status, setStatus] = useState("all")
  const [source, setSource] = useState("all")
  const [selected, setSelected] = useState<string | null>(
    new URLSearchParams(window.location.search).get("rfq")
  )
  const [detail, setDetail] = useState<Detail | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    const params = new URLSearchParams({ limit: "100" })
    if (q) params.set("q", q)
    if (status !== "all") params.set("status", status)
    if (source !== "all") params.set("source", source)
    try {
      const data = await getJson(`/admin/rfqs?${params}`)
      setRfqs(data.rfqs)
      setCount(data.count)
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    }
  }, [q, status, source])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    if (!selected) {
      setDetail(null)
      return
    }
    getJson(`/admin/rfqs/${selected}`)
      .then(setDetail)
      .catch((e) => setError((e as Error).message))
  }, [selected])

  const transition = async (id: string, next: string) => {
    try {
      await getJson(`/admin/rfqs/${id}/status`, {
        method: "POST",
        body: JSON.stringify({ status: next }),
      })
      await load()
      setDetail(await getJson(`/admin/rfqs/${id}`))
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const profile = detail?.rfq.enrichment_payload?.profile
  const trail = detail?.trail ?? detail?.rfq.enrichment_payload?.trail
  const quote = detail?.rfq.quote_payload

  return (
    <div className="flex flex-col gap-y-3">
      <Container className="p-0">
        <div className="flex flex-wrap items-center justify-between gap-3 px-6 py-4">
          <div>
            <Heading level="h1">Leads &amp; RFQs</Heading>
            <Text size="small" className="text-ui-fg-subtle">
              Instant quotes, contact-form requests, AI lead scores and the
              buyer's browsing trail.
            </Text>
          </div>
          <div className="flex flex-wrap gap-2">
            <Input
              placeholder="Search email, company, product"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              size="small"
            />
            <Select size="small" value={status} onValueChange={setStatus}>
              <Select.Trigger>
                <Select.Value placeholder="Status" />
              </Select.Trigger>
              <Select.Content>
                {[
                  "all",
                  "SUBMITTED",
                  "QUOTED",
                  "ACCEPTED",
                  "ORDERED",
                  "CLOSED",
                ].map((value) => (
                  <Select.Item key={value} value={value}>
                    {value === "all" ? "All statuses" : value}
                  </Select.Item>
                ))}
              </Select.Content>
            </Select>
            <Select size="small" value={source} onValueChange={setSource}>
              <Select.Trigger>
                <Select.Value placeholder="Source" />
              </Select.Trigger>
              <Select.Content>
                {[
                  "all",
                  "instant_quote",
                  "instant_quote_review",
                  "contact_form",
                  "project_draft",
                ].map((value) => (
                  <Select.Item key={value} value={value}>
                    {value === "all" ? "All sources" : value.replace(/_/g, " ")}
                  </Select.Item>
                ))}
              </Select.Content>
            </Select>
          </div>
        </div>
        {error && (
          <Text size="small" className="text-ui-fg-error px-6 pb-3">
            {error}
          </Text>
        )}
        <Table>
          <Table.Header>
            <Table.Row>
              <Table.HeaderCell>Created</Table.HeaderCell>
              <Table.HeaderCell>Buyer</Table.HeaderCell>
              <Table.HeaderCell>Request</Table.HeaderCell>
              <Table.HeaderCell>Quote</Table.HeaderCell>
              <Table.HeaderCell>Lead</Table.HeaderCell>
              <Table.HeaderCell>Status</Table.HeaderCell>
            </Table.Row>
          </Table.Header>
          <Table.Body>
            {rfqs.map((rfq) => (
              <Table.Row
                key={rfq.id}
                className="cursor-pointer"
                onClick={() => setSelected(rfq.id)}
              >
                <Table.Cell>
                  {new Date(rfq.created_at).toLocaleString()}
                </Table.Cell>
                <Table.Cell>
                  <div className="flex flex-col">
                    <span>{rfq.company || rfq.contact_name}</span>
                    <span className="text-ui-fg-subtle">{rfq.email}</span>
                  </div>
                </Table.Cell>
                <Table.Cell className="max-w-[320px] truncate">
                  {rfq.title}
                </Table.Cell>
                <Table.Cell>
                  {money(rfq.quoted_total, rfq.currency_code)}
                </Table.Cell>
                <Table.Cell>
                  {rfq.lead_grade ? (
                    <Badge color={gradeColor(rfq.lead_grade)} size="2xsmall">
                      {rfq.lead_grade} · {rfq.lead_score}
                    </Badge>
                  ) : (
                    "-"
                  )}
                </Table.Cell>
                <Table.Cell>
                  <Badge color={statusColor(rfq.status)} size="2xsmall">
                    {rfq.status}
                  </Badge>
                </Table.Cell>
              </Table.Row>
            ))}
          </Table.Body>
        </Table>
        <Text size="small" className="text-ui-fg-subtle px-6 py-3">
          {count} total
        </Text>
      </Container>

      {detail && (
        <Container className="flex flex-col gap-y-4 px-6 py-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <Heading level="h2">
                {detail.rfq.company || detail.rfq.contact_name}
              </Heading>
              <Text size="small" className="text-ui-fg-subtle">
                {detail.rfq.contact_name} · {detail.rfq.email}
                {detail.rfq.phone ? ` · ${detail.rfq.phone}` : ""}
                {detail.rfq.website ? ` · ${detail.rfq.website}` : ""}
              </Text>
              <Text size="small" className="text-ui-fg-subtle">
                {detail.rfq.source.replace(/_/g, " ")} · follow-ups sent:{" "}
                {detail.rfq.followup_count}
                {detail.rfq.contact_opt_out ? " · opted out of reminders" : ""}
                {detail.rfq.order_id ? ` · order ${detail.rfq.order_id}` : ""}
              </Text>
            </div>
            <div className="flex gap-2">
              {(NEXT_STATUS[detail.rfq.status] ?? []).map((next) => (
                <Button
                  key={next}
                  size="small"
                  variant={next === "CLOSED" ? "secondary" : "primary"}
                  onClick={() => transition(detail.rfq.id, next)}
                >
                  Mark {next.toLowerCase()}
                </Button>
              ))}
              <Button
                size="small"
                variant="transparent"
                onClick={() => setSelected(null)}
              >
                Close
              </Button>
            </div>
          </div>

          {profile && (
            <div className="rounded-lg border p-4">
              <div className="mb-2 flex items-center gap-2">
                <SparklesSolid />
                <Text weight="plus">
                  Lead profile ({profile.source}) · {profile.lead_grade} /{" "}
                  {profile.lead_score}
                </Text>
              </div>
              <Text size="small">{profile.company_summary}</Text>
              <Text size="small" className="mt-2">
                <strong>Industry:</strong> {profile.industry ?? "unknown"} ·{" "}
                <strong>Size:</strong> {profile.company_size_estimate}
              </Text>
              <Text size="small">
                <strong>Likely needs:</strong>{" "}
                {(profile.likely_packaging_needs ?? []).join(", ") || "-"}
              </Text>
              <Text size="small">
                <strong>Why:</strong>{" "}
                {(profile.score_reasons ?? []).join("; ") || "-"}
              </Text>
              <Text size="small">
                <strong>Next best action:</strong> {profile.next_best_action}
              </Text>
            </div>
          )}

          {quote && (
            <div className="rounded-lg border p-4">
              <Text weight="plus">
                {quote.summary} · {money(quote.total, quote.currency_code)} (
                {money(quote.unit_price, quote.currency_code)}/pc)
              </Text>
              <Text size="small" className="text-ui-fg-subtle">
                {quote.spec_labels?.material} · production{" "}
                {quote.lead_time?.production_days?.join("-")} business days ·
                delivery {quote.estimated_delivery?.from} to{" "}
                {quote.estimated_delivery?.to} · valid until {quote.valid_until}{" "}
                · pricing {quote.pricing_version}
              </Text>
              <ul className="mt-2 list-disc pl-5">
                {(quote.breakdown ?? []).map((line: any) => (
                  <li key={line.label}>
                    <Text size="small">
                      {line.label}: {money(line.amount, quote.currency_code)}
                    </Text>
                  </li>
                ))}
              </ul>
              {(detail.rfq.quotes ?? []).length > 1 && (
                <Text size="small" className="mt-2">
                  Quote rounds:{" "}
                  {(detail.rfq.quotes ?? [])
                    .map(
                      (round) =>
                        `#${round.round} ${money(round.price, quote.currency_code)} ${round.status}`
                    )
                    .join(" · ")}
                </Text>
              )}
            </div>
          )}

          {trail && trail.events > 0 && (
            <div className="rounded-lg border p-4">
              <Text weight="plus">
                Browsing trail: {trail.pages_viewed} page views ·{" "}
                {trail.quote_interactions} quote interactions · ~
                {trail.active_minutes} active min
                {trail.entry_referrer ? ` · from ${trail.entry_referrer}` : ""}
              </Text>
              <ul className="mt-2 list-disc pl-5">
                {(trail.top_pages ?? []).map((page: any) => (
                  <li key={page.path}>
                    <Text size="small">
                      {page.title || page.path} ({page.views}x)
                    </Text>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {detail.rfq.notes && (
            <Text size="small">
              <strong>Buyer notes:</strong> {detail.rfq.notes}
            </Text>
          )}
        </Container>
      )}
    </div>
  )
}

export const config = defineRouteConfig({
  label: "Leads & RFQs",
  icon: SparklesSolid,
})

export default LeadsPage
