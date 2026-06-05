import ContactRfqForm from "../components/contact-rfq-form"
import styles from "./contact-template.module.css"

const faqLinks = [
  {
    href: "https://support.packoasis.com/portal/en/kb/articles/what-is-the-minimum-size-of-an-order",
    label: "What is your minimum order quantities (MOQs)?",
  },
  {
    href: "https://support.packoasis.com/portal/en/kb/articles/what-is-the-turnaround-time-on-my-order",
    label: "What is the turnaround time on my order?",
  },
  {
    href: "https://support.packoasis.com/portal/en/kb/articles/where-do-you-ship-to",
    label: "Where do you ship to?",
  },
  {
    href: "https://support.packoasis.com/portal/en/kb/articles/do-you-have-volume-discounts-or-price-breaks",
    label: "Do you have volume discounts or price breaks?",
  },
  {
    href: "https://support.packoasis.com/portal/en/kb/articles/do-you-offer-rush-orders",
    label: "Do you offer rush orders?",
  },
  {
    href: "https://support.packoasis.com/portal/en/kb/articles/how-do-i-place-a-reorder",
    label: "How do I place a reorder?",
  },
  {
    href: "https://support.packoasis.com/portal/en/home",
    label: "I have more questions",
  },
]

const facilities = [
  {
    region: "United States",
    cities: "Seattle · New York · Corona · Los Angeles · Chicago",
  },
  {
    region: "Canada",
    cities: "Toronto · North York · Markham · Scarborough",
  },
  {
    region: "East Asia",
    cities: "Guangzhou · Shenzhen · Dongguan · Shanghai · Taiwan",
  },
  {
    region: "South & Southeast Asia",
    cities: "India · Vietnam · Thailand · Cambodia",
  },
]

type ContactTemplateProps = {
  draft?: {
    items?: Array<{
      title?: string
      quantity?: number
      notes?: string
    }>
  } | null
}

export default function ContactTemplate({ draft }: ContactTemplateProps) {
  const primaryDraftItem = draft?.items?.[0]
  const hasDraftItems = Boolean(draft?.items?.length)

  return (
    <div className={styles.page}>
      <link
        rel="stylesheet"
        href="/static.packoasis.com/version1775029070/frontend/Smartwave/porto_child/en_US/css/smart-forms/smart-forms.css"
      />
      <link
        rel="stylesheet"
        href="/static.packoasis.com/version1775029070/frontend/Smartwave/porto_child/en_US/bootstrap-5.0.0-beta2-dist/css/bootstrap.css"
      />
      <link
        rel="stylesheet"
        href="/media.packoasis.com/porto/web/css/custom.css"
      />
      <link
        rel="stylesheet"
        href="/media.packoasis.com/porto/web/css/coding-guide.css"
      />
      <link
        rel="stylesheet"
        href="/media.packoasis.com/porto/configed_css/design_english.css"
      />
      <link
        rel="stylesheet"
        href="/media.packoasis.com/porto/configed_css/settings_english.css"
      />
      <link
        rel="stylesheet"
        href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/4.7.0/css/font-awesome.min.css"
      />
      <div id="contact-us-form" className="landing-page">
        <div className="container">
          <div className="row">
            <div className="col-md-6 flex">
              <div className="section-leading-des">
                <div className="icon-group">
                  <div className="icon-img">
                    <img
                      src="/media.packoasis.com/media_upload/coding_guide/customer-company-icons.svg"
                      alt="customer brands"
                    />
                  </div>
                  <div className="icon-des">
                    Join 3,000+ brands and experience
                    <br className="br-hid-ld" />
                    the <a href="/us">PackOasis difference</a>!
                  </div>
                  <div className="clear" />
                </div>

                <h1>Your Packaging Success Starts with PackOasis</h1>
                <p style={{ fontSize: 18, lineHeight: "150%" }} className="hid-xs">
                  At PackOasis, we strive to provide superior services and
                  solutions that surpass your expectations. Let us find the
                  ideal packaging solution for your project.{" "}
                  <a href="#contact-us" style={{ color: "#40C173" }}>
                    Got a Question?
                  </a>
                </p>

                <div className="form-faq hid-xs">
                  <h3>Frequently Asked Questions</h3>
                  <ul>
                    {faqLinks.map((item) => (
                      <li key={item.href}>
                        <a href={item.href} target="_blank" rel="noreferrer">
                          {item.label}
                        </a>
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            </div>

            <div className="col-md-6 flex">
              <div id="send-inquiry" className="lead-form-section">
                <h2>REQUEST A QUOTE</h2>
                <p>
                  Complete our quote request form or email us at{" "}
                  <a
                    href="mailto:hello@packoasis.com"
                    className="underline-hover"
                  >
                    hello@packoasis.com
                  </a>{" "}
                  to receive a customized quote from our product specialists.
                </p>
                <div className="smart-forms smart-container wrap-4">
                  <div id="crmWebToEntityForm">
                    <ContactRfqForm
                      initialProduct={primaryDraftItem?.title}
                      initialQuantity={primaryDraftItem?.quantity}
                      initialDescription={primaryDraftItem?.notes}
                      hasDraftItems={hasDraftItems}
                    />
                  </div>
                </div>
              </div>
            </div>

            <div className="col-md-6 flex hid-lg hid-md">
              <div className="form-faq">
                <h3>Frequently Asked Questions</h3>
                <ul>
                  {faqLinks.slice(0, 6).map((item) => (
                    <li key={item.href}>
                      <a href={item.href} target="_blank" rel="noreferrer">
                        {item.label}
                      </a>
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          </div>
        </div>

        <section id="contact-us" style={{ backgroundColor: "white" }}>
          <div className="container contact-type">
            <div className="row align-items-center justify-content-between">
              <div className="col-md-6 col-lg-5 order-md-2">
                <div className="contact-des">
                  <div className="upline">LIVE SUPPORT</div>
                  <h3>
                    Looking for instant support? Speak with our experts within a
                    minute.
                  </h3>
                  <p>
                    Get in contact with our packaging experts in a matter of
                    minutes for direct support to your packaging needs.
                  </p>
                  <div className="row">
                    <div className="col-md-6">
                      <h4>Toll-free Call Center</h4>
                      <div className="contact-list">
                        <img
                          src="/media.packoasis.com/media_upload/coding_guide/icon-phone-a.svg"
                          alt="Call"
                        />
                        <a href="tel:1-888-622-2819">1-888-622-2819</a>
                      </div>
                      <div className="contact-list">
                        <img
                          src="/media.packoasis.com/media_upload/coding_guide/icon-calendar-a.svg"
                          alt="Calendar"
                        />
                        Monday - Friday
                      </div>
                      <div className="contact-list">
                        <img
                          src="/media.packoasis.com/media_upload/coding_guide/icon-clock-a.svg"
                          alt="Clock"
                        />
                        9:30 AM - 6:30 PM EST
                      </div>
                    </div>
                    <div className="col-md-6">
                      <h4>Sales Inquiries</h4>
                      <div className="contact-list">
                        <img
                          src="/media.packoasis.com/media_upload/coding_guide/icon-mail-a.svg"
                          alt="Mail"
                        />
                        <a href="mailto:hello@packoasis.com">
                          hello@packoasis.com
                        </a>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
              <div className="col-md-6 order-md-first">
                <img
                  src="/media.packoasis.com/media_upload/coding_guide/consultation-support.webp"
                  alt="customer support"
                  className="mobile-space-37"
                  style={{ borderRadius: 10 }}
                />
              </div>
            </div>
          </div>
        </section>

        <section>
          <div className="container contact-type">
            <div className="row align-items-center justify-content-between">
              <div className="col-md-6 col-lg-5">
                <div className="contact-des">
                  <h3>Our sales office</h3>
                  <p>
                    Our office is walk-in by appointment only. Please call us at{" "}
                    <a href="tel:1-888-622-2819" style={{ color: "#353a3f" }}>
                      1-888-622-2819
                    </a>{" "}
                    to book an appointment.
                  </p>
                  <h4>Walk-in Sales Office</h4>
                  <div className="contact-list contact-add">
                    600 - 600 Alden Road
                    <br />
                    Markham, ON L3R 0E7
                    <br />
                    Canada
                  </div>

                  <h3>Our manufacturing facilities around the world</h3>
                  <div className="row">
                    {facilities.map((facility) => (
                      <div key={facility.region} className="col-md-6">
                        <h4>{facility.region}</h4>
                        <div className="contact-list">{facility.cities}</div>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
              <div className="col-md-6">
                <img
                  src="/media.packoasis.com/media_upload/coding_guide/packoasis-business.webp"
                  alt="sales office"
                  className="mobile-space-37"
                  style={{ borderRadius: 10 }}
                />
              </div>
            </div>
          </div>
        </section>
      </div>
    </div>
  )
}
