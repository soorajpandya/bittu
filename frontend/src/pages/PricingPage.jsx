import { useState } from "react";
import { Check, Star, Printer, ChevronDown, ChevronUp, ArrowRight, Shield, Zap, Users } from "lucide-react";

// ─── Plan Data ──────────────────────────────────────────────

const PLANS = [
  {
    slug: "starter",
    name: "Starter",
    price: 2999,
    monthlyEquiv: 250,
    description: "Perfect for small restaurants getting started",
    cta: "Start Free Trial",
    highlight: false,
    discountLabel: null,
    features: [
      "Billing + KOT",
      "AI Menu Upload",
      "Voice Billing",
      "Table Management",
      "Basic Reports",
    ],
  },
  {
    slug: "growth",
    name: "Growth",
    price: 5999,
    monthlyEquiv: 500,
    description: "Everything you need to grow your business",
    cta: "Start My Setup",
    highlight: true,
    discountLabel: "Save ₹1,500",
    features: [
      "Everything in Starter",
      "WhatsApp Marketing",
      "Customer Loyalty System",
      "Stock Management",
      "Advanced Reports",
      "Google Business Profile",
      "Priority Support",
    ],
  },
  {
    slug: "pro",
    name: "Pro",
    price: 9999,
    monthlyEquiv: 834,
    description: "For multi-branch restaurants at scale",
    cta: "Scale My Business",
    highlight: false,
    discountLabel: null,
    features: [
      "Everything in Growth",
      "Multi-device Login",
      "Multi-branch Support",
      "Advanced Analytics Dashboard",
      "Dedicated Onboarding",
      "Premium WhatsApp Automation",
    ],
  },
];

const FAQS = [
  {
    q: "Is there a free trial?",
    a: "Yes! Every plan comes with a 14-day free trial. No credit card required to start.",
  },
  {
    q: "Can I switch plans later?",
    a: "Absolutely. Upgrades take effect immediately. Downgrades apply at the next billing cycle.",
  },
  {
    q: "What happens when my subscription expires?",
    a: "You get a 3-day grace period. After that, access is paused until you renew. Your data is never deleted.",
  },
  {
    q: "Do you offer monthly billing?",
    a: "Currently, all plans are billed annually for the best value. Monthly billing is coming soon.",
  },
  {
    q: "Can I get a refund?",
    a: "We offer a 7-day money-back guarantee. If you're not satisfied, contact us for a full refund.",
  },
  {
    q: "Is the printer included with any plan?",
    a: "The thermal printer is a one-time add-on purchase (₹2,999). It works with all plans.",
  },
];

// ─── API Integration ────────────────────────────────────────

const API_BASE = import.meta.env.VITE_API_URL || "https://api.merabittu.com";

async function subscribeToPlan(planSlug, token) {
  const res = await fetch(`${API_BASE}/api/v1/subscriptions/subscribe`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ plan_slug: planSlug }),
  });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || "Failed to create subscription");
  }
  return res.json();
}

async function startFreeTrial(token) {
  const res = await fetch(`${API_BASE}/api/v1/subscriptions/free-trial`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
  });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || "Failed to start trial");
  }
  return res.json();
}

// ─── Components ─────────────────────────────────────────────

function PlanCard({ plan, onSubscribe, loading }) {
  const isGrowth = plan.highlight;

  return (
    <div
      className={`relative flex flex-col rounded-2xl border-2 p-8 transition-all duration-300 hover:shadow-xl ${
        isGrowth
          ? "border-indigo-500 bg-white shadow-lg scale-[1.02] ring-1 ring-indigo-500/20"
          : "border-gray-200 bg-white hover:border-gray-300"
      }`}
    >
      {/* Most Popular Badge */}
      {isGrowth && (
        <div className="absolute -top-4 left-1/2 -translate-x-1/2">
          <span className="inline-flex items-center gap-1.5 rounded-full bg-indigo-600 px-4 py-1.5 text-sm font-semibold text-white shadow-md">
            <Star className="h-3.5 w-3.5 fill-current" />
            Most Popular
          </span>
        </div>
      )}

      {/* Discount Badge */}
      {plan.discountLabel && (
        <span className="mb-4 inline-block w-fit rounded-md bg-green-50 px-3 py-1 text-sm font-medium text-green-700 ring-1 ring-green-200">
          {plan.discountLabel}
        </span>
      )}

      {/* Plan Name */}
      <h3 className="text-xl font-bold text-gray-900">{plan.name}</h3>
      <p className="mt-1 text-sm text-gray-500">{plan.description}</p>

      {/* Price */}
      <div className="mt-6">
        <div className="flex items-baseline gap-1">
          <span className="text-4xl font-extrabold tracking-tight text-gray-900">
            ₹{plan.price.toLocaleString("en-IN")}
          </span>
          <span className="text-base font-medium text-gray-500">/year</span>
        </div>
        <p className="mt-1 text-sm text-gray-400">
          ₹{plan.monthlyEquiv}/month equivalent
        </p>
      </div>

      {/* CTA Button */}
      <button
        onClick={() => onSubscribe(plan.slug)}
        disabled={loading}
        className={`mt-8 flex w-full items-center justify-center gap-2 rounded-xl px-6 py-3.5 text-base font-semibold transition-all duration-200 disabled:opacity-50 ${
          isGrowth
            ? "bg-indigo-600 text-white hover:bg-indigo-700 shadow-md hover:shadow-lg"
            : "bg-gray-900 text-white hover:bg-gray-800"
        }`}
      >
        {loading ? (
          <div className="h-5 w-5 animate-spin rounded-full border-2 border-white border-t-transparent" />
        ) : (
          <>
            {plan.cta}
            <ArrowRight className="h-4 w-4" />
          </>
        )}
      </button>

      {/* Features */}
      <ul className="mt-8 space-y-3">
        {plan.features.map((feature, i) => (
          <li key={i} className="flex items-start gap-3">
            <Check
              className={`h-5 w-5 flex-shrink-0 mt-0.5 ${
                isGrowth ? "text-indigo-600" : "text-green-500"
              }`}
            />
            <span className="text-sm text-gray-700">{feature}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function PrinterAddon() {
  return (
    <div className="mx-auto mt-20 max-w-2xl rounded-2xl border border-gray-200 bg-gradient-to-br from-gray-50 to-white p-8 shadow-sm">
      <div className="flex flex-col items-center gap-6 sm:flex-row">
        <div className="flex h-20 w-20 items-center justify-center rounded-2xl bg-indigo-50">
          <Printer className="h-10 w-10 text-indigo-600" />
        </div>
        <div className="flex-1 text-center sm:text-left">
          <h3 className="text-lg font-bold text-gray-900">
            🖨️ Add a Thermal Printer
          </h3>
          <p className="mt-1 text-sm text-gray-500">
            2-inch Bluetooth + USB receipt printer with 1-year warranty. Free shipping!
          </p>
        </div>
        <div className="text-center">
          <p className="text-2xl font-extrabold text-gray-900">₹2,999</p>
          <p className="text-xs text-gray-400">one-time purchase</p>
          <button className="mt-3 rounded-lg border border-indigo-600 px-6 py-2 text-sm font-semibold text-indigo-600 transition-colors hover:bg-indigo-50">
            Add to Order
          </button>
        </div>
      </div>
    </div>
  );
}

function FAQSection() {
  const [openIndex, setOpenIndex] = useState(null);

  return (
    <div className="mx-auto mt-24 max-w-3xl">
      <h2 className="text-center text-3xl font-bold text-gray-900">
        Frequently Asked Questions
      </h2>
      <div className="mt-10 space-y-3">
        {FAQS.map((faq, i) => (
          <div
            key={i}
            className="rounded-xl border border-gray-200 bg-white transition-all"
          >
            <button
              onClick={() => setOpenIndex(openIndex === i ? null : i)}
              className="flex w-full items-center justify-between px-6 py-5 text-left"
            >
              <span className="text-base font-medium text-gray-900">
                {faq.q}
              </span>
              {openIndex === i ? (
                <ChevronUp className="h-5 w-5 text-gray-400" />
              ) : (
                <ChevronDown className="h-5 w-5 text-gray-400" />
              )}
            </button>
            {openIndex === i && (
              <div className="px-6 pb-5">
                <p className="text-sm leading-relaxed text-gray-600">{faq.a}</p>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function TrustBadges() {
  return (
    <div className="mt-16 flex flex-wrap items-center justify-center gap-8 text-sm text-gray-500">
      <div className="flex items-center gap-2">
        <Shield className="h-5 w-5 text-green-500" />
        <span>256-bit SSL Encrypted</span>
      </div>
      <div className="flex items-center gap-2">
        <Zap className="h-5 w-5 text-amber-500" />
        <span>Setup in 5 minutes</span>
      </div>
      <div className="flex items-center gap-2">
        <Users className="h-5 w-5 text-indigo-500" />
        <span>Trusted by 1000+ restaurants</span>
      </div>
    </div>
  );
}

// ─── Main Page ──────────────────────────────────────────────

export default function PricingPage() {
  const [loadingPlan, setLoadingPlan] = useState(null);

  const handleSubscribe = async (planSlug) => {
    setLoadingPlan(planSlug);
    try {
      // Get token from your auth context/store
      const token = localStorage.getItem("access_token") || "";

      if (!token) {
        // Redirect to login with return URL
        window.location.href = `/login?redirect=/pricing&plan=${planSlug}`;
        return;
      }

      if (planSlug === "starter") {
        // Starter plan starts with free trial
        try {
          await startFreeTrial(token);
          window.location.href = "/dashboard?trial=started";
          return;
        } catch {
          // If trial already used, proceed to paid subscription
        }
      }

      const result = await subscribeToPlan(planSlug, token);

      if (result.short_url) {
        // Redirect to Razorpay checkout
        window.location.href = result.short_url;
      } else if (result.razorpay_subscription_id) {
        // Open Razorpay inline checkout
        openRazorpayCheckout(result);
      }
    } catch (error) {
      alert(error.message || "Something went wrong. Please try again.");
    } finally {
      setLoadingPlan(null);
    }
  };

  const openRazorpayCheckout = (subData) => {
    if (typeof window.Razorpay === "undefined") {
      // Fallback to short_url
      if (subData.short_url) {
        window.location.href = subData.short_url;
      }
      return;
    }
    const options = {
      key: import.meta.env.VITE_RAZORPAY_KEY_ID,
      subscription_id: subData.razorpay_subscription_id,
      name: "Bittu",
      description: `${subData.plan?.name || ""} Plan Subscription`,
      handler: function (response) {
        // Payment successful — redirect to dashboard
        window.location.href = `/dashboard?subscription=activated&payment_id=${response.razorpay_payment_id}`;
      },
      theme: { color: "#4F46E5" },
    };
    const rzp = new window.Razorpay(options);
    rzp.open();
  };

  return (
    <div className="min-h-screen bg-gradient-to-b from-slate-50 via-white to-slate-50">
      {/* Header */}
      <div className="px-4 pt-20 pb-4 text-center">
        <div className="mx-auto mb-6 inline-flex items-center gap-2 rounded-full bg-indigo-50 px-4 py-2 text-sm font-medium text-indigo-700 ring-1 ring-indigo-100">
          <Zap className="h-4 w-4" />
          14-day free trial on all plans
        </div>
        <h1 className="text-4xl font-extrabold tracking-tight text-gray-900 sm:text-5xl lg:text-6xl">
          Run Your Restaurant on{" "}
          <span className="bg-gradient-to-r from-indigo-600 to-violet-600 bg-clip-text text-transparent">
            Autopilot
          </span>
        </h1>
        <p className="mx-auto mt-4 max-w-2xl text-lg text-gray-500">
          Billing, KOT, stock &amp; marketing — all in one system.
          <br className="hidden sm:block" />
          Join 1,000+ restaurants already on Bittu.
        </p>
      </div>

      {/* Pricing Cards */}
      <div className="mx-auto mt-12 grid max-w-6xl gap-8 px-4 sm:px-6 lg:grid-cols-3 lg:px-8">
        {PLANS.map((plan) => (
          <PlanCard
            key={plan.slug}
            plan={plan}
            onSubscribe={handleSubscribe}
            loading={loadingPlan === plan.slug}
          />
        ))}
      </div>

      {/* Trust Badges */}
      <TrustBadges />

      {/* Printer Add-on */}
      <div className="px-4">
        <PrinterAddon />
      </div>

      {/* FAQ */}
      <div className="px-4">
        <FAQSection />
      </div>

      {/* Need Help CTA */}
      <div className="mx-auto mt-20 mb-20 max-w-xl px-4 text-center">
        <div className="rounded-2xl bg-indigo-600 px-8 py-10 text-white shadow-xl">
          <h3 className="text-2xl font-bold">Need help choosing?</h3>
          <p className="mt-2 text-indigo-100">
            Our team will help you pick the right plan for your restaurant.
          </p>
          <a
            href="https://wa.me/919876543210?text=Hi%2C%20I%20need%20help%20choosing%20a%20Bittu%20plan"
            target="_blank"
            rel="noopener noreferrer"
            className="mt-6 inline-flex items-center gap-2 rounded-xl bg-white px-8 py-3.5 text-base font-semibold text-indigo-600 transition-all hover:bg-indigo-50"
          >
            Chat with us on WhatsApp
            <ArrowRight className="h-4 w-4" />
          </a>
        </div>
      </div>
    </div>
  );
}
