/** @odoo-module **/
import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const PERIODS = [
    ["month", "This month"],
    ["30d", "Last 30 days"],
    ["year", "This year"],
    ["all", "All time"],
];

export class EinvoiceDashboard extends Component {
    static template = "ae_einvoice.Dashboard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.actionService = useService("action");
        this.periods = PERIODS;
        this.state = useState({ period: "month", data: null });
        onWillStart(() => this.load());
    }

    async load() {
        this.state.data = await this.orm.call("einvoice.dashboard", "get_data", [this.state.period]);
    }

    async onPeriodChange(ev) {
        this.state.period = ev.target.value;
        await this.load();
    }

    async open(side, key) {
        const action = await this.orm.call("einvoice.dashboard", "open_list", [side, key, this.state.period]);
        await this.actionService.doAction(action);
    }

    onCardKeydown(ev, side, key) {
        if (ev.key === "Enter" || ev.key === " ") {
            ev.preventDefault();
            this.open(side, key);
        }
    }

    pct(count, total) {
        return total ? Math.round((count * 100) / total) : 0;
    }

    rateLabel(rate) {
        return rate === null || rate === false ? "n/a" : `${rate}%`;
    }

    // One entry per side; the template renders both with the same markup.
    get sections() {
        const d = this.state.data;
        if (!d) {
            return [];
        }
        const { ar, ap } = d;
        return [
            {
                side: "ar",
                title: "Sales (AR) - submitted to Cygnet",
                total: ar.total,
                totalLabel: "In E-Invoice workspace",
                rate: ar.rate,
                cards: [
                    { key: "sent", label: "Submitted", value: ar.sent, variant: "primary" },
                    { key: "accepted", label: "Accepted", value: ar.accepted, variant: "success" },
                    { key: "rejected", label: "Rejected", value: ar.rejected, variant: "danger" },
                    { key: "pending", label: "Pending", value: ar.pending, variant: "warning" },
                    { key: "not_sent", label: "Not yet submitted", value: ar.not_sent, variant: "secondary" },
                ],
                bar: [
                    { label: "Accepted", value: ar.accepted, variant: "success" },
                    { label: "Pending", value: ar.pending, variant: "warning" },
                    { label: "Rejected", value: ar.rejected, variant: "danger" },
                ],
                barTotal: ar.sent,
                extras: [
                    { title: "Peppol MLS (C3)", prefix: "peppol", counts: ar.peppol },
                    { title: "Tax Authority MLS (FTA C5)", prefix: "fta", counts: ar.fta },
                ],
            },
            {
                side: "ap",
                title: "Purchases (AP) - fetched from Cygnet",
                total: ap.fetched,
                totalLabel: "Fetched",
                rate: ap.rate,
                note: ap.last_fetched_up_to ? `Fetched up to ${ap.last_fetched_up_to} (UTC)` : "",
                cards: [
                    { key: "all", label: "Fetched", value: ap.fetched, variant: "primary" },
                    { key: "accepted", label: "Accepted (matched / billed)", value: ap.accepted, variant: "success" },
                    { key: "rejected", label: "Rejected", value: ap.rejected, variant: "danger" },
                    { key: "pending", label: "Awaiting review", value: ap.pending, variant: "warning" },
                    { key: "billed", label: "Vendor bills released", value: ap.billed, variant: "secondary" },
                ],
                bar: [
                    { label: "Accepted", value: ap.accepted, variant: "success" },
                    { label: "Awaiting review", value: ap.pending, variant: "warning" },
                    { label: "Rejected", value: ap.rejected, variant: "danger" },
                ],
                barTotal: ap.fetched,
                extras: [],
            },
        ];
    }
}

registry.category("actions").add("ae_einvoice.dashboard", EinvoiceDashboard);
