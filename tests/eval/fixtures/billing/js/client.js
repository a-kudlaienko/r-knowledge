"use strict";

const MAX_RETRIES = 3;

async function fetchWithRetry(url, attempt = 1) {
  // Retry a fetch call up to MAX_RETRIES times on failure.
  try {
    return await fetch(url);
  } catch (err) {
    if (attempt >= MAX_RETRIES) {
      throw err;
    }
    return fetchWithRetry(url, attempt + 1);
  }
}

module.exports = { fetchWithRetry };
