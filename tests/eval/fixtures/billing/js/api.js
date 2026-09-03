"use strict";

const { fetchWithRetry } = require("./client");

const INVOICE_ENDPOINT = "/api/invoices";

function handleInvoiceRequest(invoiceId) {
  // Look up an invoice by id and return its billing status.
  return fetchWithRetry(`${INVOICE_ENDPOINT}/${invoiceId}`);
}

class InvoiceClient {
  constructor(baseUrl) {
    this.baseUrl = baseUrl;
  }

  async fetchInvoice(invoiceId) {
    return fetchWithRetry(`${this.baseUrl}${INVOICE_ENDPOINT}/${invoiceId}`);
  }
}

module.exports = { handleInvoiceRequest, InvoiceClient };
