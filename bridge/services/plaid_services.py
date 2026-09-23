import plaid
from plaid.api import plaid_api
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.country_code import CountryCode
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.products import Products
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest

# Map user string config to official Plaid SDK environment endpoints
ENV_MAP = {
    "sandbox": plaid.Environment.Sandbox,
    "production": plaid.Environment.Production,
}


class PlaidService:
    def __init__(self, user_id: str, client_id: str, secret: str, env: str = "sandbox"):
        configuration = plaid.Configuration(
            host=ENV_MAP.get(env.lower(), plaid.Environment.Sandbox),
            api_key={"clientId": client_id, "secret": secret},
        )

        api_client = plaid.ApiClient(configuration)
        self.client = plaid_api.PlaidApi(api_client)

        self.user_id = user_id

    @classmethod
    def from_config(cls, config):
        """Factory method to construct the service directly from the DB model."""
        return cls(
            user_id=config.user_id,
            client_id=config.plaid_client_id,
            secret=config.plaid_secret,
            env=config.plaid_env,
        )

    def create_link_token(self) -> str:
        """Generates an ephemeral link_token required to initialize Plaid Link on the frontend."""

        request = LinkTokenCreateRequest(
            client_name="Actual Budget Bridge",
            language="en",
            country_codes=[CountryCode("US")],
            products=[Products("transactions")],
            user=LinkTokenCreateRequestUser(client_user_id=self.user_id),
        )

        response = self.client.link_token_create(request)
        return response["link_token"]

    def exchange_public_token(
        self,
        public_token: str,
    ) -> tuple[str, str]:
        """
        Exchanges the one-time public_token from Plaid Link for a
        permanent access_token and item_id.

        Returns:
            (access_token, item_id)
        """

        request = ItemPublicTokenExchangeRequest(public_token=public_token)
        response = self.client.item_public_token_exchange(request)
        return response["access_token"], response["item_id"]
