/**
 * New Relic agent configuration.
 *
 * See lib/config.defaults.js in the agent distribution for a more complete
 * description of configuration variables and their potential values.
 */

var config = require('./config');

exports.config = {
  /**
   * Array of application names.
   */
  app_name: [config.newrelic.app_name],
  /**
   * Your New Relic license key.
   */
  license_key: config.newrelic.license_key,
  logging: {
    /**
     * Level at which to log. 'trace' is most useful to New Relic when diagnosing
     * issues with the agent, 'info' and higher will impose the least overhead on
     * production applications.
     */
    level: config.newrelic.log_level
  }
};
