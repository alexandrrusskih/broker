const { setTimeout } = require("timers/promises");

const isRetryableStatus = (response, retryableStatusCodes) => {
  if (!response || response.ok) return false;

  const isClientError = response.status >= 400 && response.status < 500;
  if (isClientError) return false;

  if (!retryableStatusCodes) return true;

  if (retryableStatusCodes instanceof Set) {
    return retryableStatusCodes.has(response.status);
  }

  if (Array.isArray(retryableStatusCodes)) {
    return retryableStatusCodes.includes(response.status);
  }

  return false;
};

exports.fetchWithRetry = async (
  url,
  options,
  retries = 3,
  backoff = 300,
  retryOptions = {}
) => {
  const {
    retryableStatusCodes = null,
    onRetry,
    includeAttempts = false,
    throwOnHttpError = true
  } = retryOptions;
  const totalAttempts = retries + 1;
  let nextBackoff = backoff;

  for (let attempt = 1; attempt <= totalAttempts; attempt++) {
    try {
      const response = await fetch(url, options);

      if (response.ok) {
        return includeAttempts ? { response, attempts: attempt } : response;
      }

      const shouldRetry =
        attempt < totalAttempts && isRetryableStatus(response, retryableStatusCodes);

      if (shouldRetry) {
        if (typeof onRetry === "function") {
          await onRetry({
            attempt,
            nextAttempt: attempt + 1,
            totalAttempts,
            backoffMs: nextBackoff,
            response
          });
        }
        await setTimeout(nextBackoff);
        nextBackoff *= 2;
        continue;
      }

      if (!throwOnHttpError) {
        return includeAttempts ? { response, attempts: attempt } : response;
      }

      const error = new Error(`Request failed with status ${response.status}`);
      error.isClientError = response.status >= 400 && response.status < 500;
      error.response = response;
      error.attempts = attempt;
      throw error;
    } catch (error) {
      if (error.isClientError) {
        error.attempts = error.attempts || attempt;
        throw error;
      }

      if (attempt < totalAttempts) {
        if (typeof onRetry === "function") {
          await onRetry({
            attempt,
            nextAttempt: attempt + 1,
            totalAttempts,
            backoffMs: nextBackoff,
            error
          });
        }
        await setTimeout(nextBackoff);
        nextBackoff *= 2;
        continue;
      }

      error.attempts = error.attempts || attempt;
      throw error;
    }
  }
};
