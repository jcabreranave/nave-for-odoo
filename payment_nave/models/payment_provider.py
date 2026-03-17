import logging
import time

import requests

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

# ── URLs por entorno ──────────────────────────────────────────────────────────

_AUTH_URL = {
    'prod':    'https://services.apinaranja.com/security-ms/api/security/auth0/b2b/m2msPrivate',
    'sandbox': 'https://homoservices.apinaranja.com/security-ms/api/security/auth0/b2b/m2ms',
}
_API_URL = {
    'prod':    'https://api.ranty.io/api',
    'sandbox': 'https://api-sandbox.ranty.io/api',
}
_PAYMENTS_URL = {
    'prod':    'https://punku.ranty.io/payments-ms/payments',
    'sandbox': 'https://punku-sandbox.ranty.io/payments-ms/payments',
}
_AUDIENCE = 'https://naranja.com/ranty/merchants/api'

# Timeout HTTP en segundos
_TIMEOUT = 30


class PaymentProvider(models.Model):
    _inherit = 'payment.provider'

    code = fields.Selection(
        selection_add=[('nave', 'Nave')],
        ondelete={'nave': 'set default'},
    )

    # ── Credenciales ─────────────────────────────────────────────────────────

    nave_client_id = fields.Char(
        string='Client ID',
        help='Client ID de Nave B2B.',
        required_if_provider='nave',
        groups='base.group_system',
    )
    nave_client_secret = fields.Char(
        string='Client Secret',
        help='Client Secret de Nave B2B.',
        required_if_provider='nave',
        groups='base.group_system',
    )
    nave_pos_id = fields.Char(
        string='POS ID',
        help='Identificador del punto de venta asignado por Nave.',
        required_if_provider='nave',
        groups='base.group_system',
    )
    nave_duration_time = fields.Integer(
        string='Duración del link (segundos)',
        default=900,
        help='Tiempo en segundos que el link de pago de Nave estará vigente. Mínimo: 60.',
    )

    # ── Restricciones ────────────────────────────────────────────────────────

    @api.constrains('nave_duration_time')
    def _check_nave_duration_time(self):
        for rec in self:
            if rec.code == 'nave' and rec.nave_duration_time < 60:
                raise ValidationError(_('La duración mínima del link de Nave es 60 segundos.'))

    # ── Compatibilidad ───────────────────────────────────────────────────────

    def _is_compatible_with_currency(self, currency):
        """Nave opera en ARS. Aceptar cualquier moneda para no bloquear instalaciones
        con moneda distinta, pero loggear advertencia."""
        if self.code != 'nave':
            return super()._is_compatible_with_currency(currency)
        if currency.name != 'ARS':
            _logger.warning(
                'Nave: la moneda %s puede no estar soportada. Nave opera en ARS.',
                currency.name,
            )
        return True

    # ── Token management ─────────────────────────────────────────────────────

    def _nave_env(self):
        """Devuelve 'prod' o 'sandbox' según el estado del proveedor."""
        return 'prod' if self.state == 'enabled' else 'sandbox'

    def _nave_get_token(self):
        """
        Obtiene un access token válido desde caché (ir.config_parameter) o pide uno nuevo.
        Equivalente a TokenManager.get_token() del plugin de WooCommerce.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        token  = ICP.get_param(f'nave.token.{self.id}')
        expiry = float(ICP.get_param(f'nave.token_expiry.{self.id}', '0'))

        if token and expiry > time.time() + 60:
            _logger.debug('Nave: token obtenido desde caché.')
            return token

        return self._nave_fetch_new_token()

    def _nave_fetch_new_token(self):
        """Solicita un nuevo access token a la API de autenticación de Nave."""
        url = _AUTH_URL[self._nave_env()]
        try:
            resp = requests.post(
                url,
                json={
                    'client_id':     self.nave_client_id,
                    'client_secret': self.nave_client_secret,
                    'audience':      _AUDIENCE,
                },
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            raise ValidationError(
                _('Nave: no se pudo conectar al servidor de autenticación. %s') % str(e)
            )

        if not data.get('access_token'):
            raise ValidationError(_('Nave: la respuesta de autenticación no contiene access_token.'))

        token      = data['access_token']
        expires_in = int(data.get('expires_in', 3600))
        ttl        = max(expires_in - 60, 60)

        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param(f'nave.token.{self.id}', token)
        ICP.set_param(f'nave.token_expiry.{self.id}', str(time.time() + ttl))

        _logger.debug('Nave: nuevo token obtenido y cacheado por %d segundos.', ttl)
        return token

    # ── HTTP helpers ─────────────────────────────────────────────────────────

    def _nave_request(self, method, url, json=None):
        """
        Wrapper HTTP con retry automático en 401 (token expirado).
        Equivalente a NaveApiClient.request() del plugin de WooCommerce.
        """
        token = self._nave_get_token()
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type':  'application/json',
            'Accept':        'application/json',
        }

        try:
            resp = requests.request(method, url, json=json, headers=headers, timeout=_TIMEOUT)

            # Token expirado — refrescar y reintentar una vez
            if resp.status_code == 401:
                _logger.warning('Nave: token expirado (401), refrescando...')
                token = self._nave_fetch_new_token()
                headers['Authorization'] = f'Bearer {token}'
                resp = requests.request(method, url, json=json, headers=headers, timeout=_TIMEOUT)

            if not resp.ok:
                raise ValidationError(
                    _('Nave: la API respondió con HTTP %d.') % resp.status_code
                )

            return resp.json() if resp.content else {}

        except requests.RequestException as e:
            raise ValidationError(_('Nave: error de red. %s') % str(e))

    # ── API methods ───────────────────────────────────────────────────────────

    def _nave_create_payment_request(self, tx):
        """
        Crea una intención de pago en Nave.
        Equivalente a NaveApiClient.create_payment_request() del plugin de WooCommerce.
        """
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '').rstrip('/')

        callback_token  = tx._nave_generate_callback_token()
        webhook_secret  = tx._nave_generate_webhook_secret()

        callback_url = (
            f'{base_url}/nave/return'
            f'?tx_ref={tx.reference}'
            f'&nave_token={callback_token}'
        )
        notification_url = (
            f'{base_url}/nave/webhook'
            f'?tx_ref={tx.reference}'
            f'&secret={webhook_secret}'
        )

        currency = tx.currency_id.name
        order    = tx.sale_order_ids[:1]

        body = {
            'external_payment_id': tx.reference,
            'seller':              {'pos_id': self.nave_pos_id},
            'transactions':        [{
                'amount':   {'currency': currency, 'value': f'{tx.amount:.2f}'},
                'products': self._nave_build_products(order, currency),
            }],
            'buyer':          self._nave_build_buyer(tx),
            'additional_info': {
                'callback_url':     callback_url,
                'notification_url': notification_url,
            },
            'platform': {
                'id':   'odoo',
                'type': 'mktplace',
                'data': {
                    'callback_url':     callback_url,
                    'notification_url': notification_url,
                },
            },
            'duration_time': self.nave_duration_time,
        }

        url      = f'{_API_URL[self._nave_env()]}/payment_request/ecommerce'
        response = self._nave_request('POST', url, json=body)

        if not response.get('id') or not response.get('checkout_url'):
            raise ValidationError(
                _('Nave: la respuesta de creación de intención de pago es inválida (faltan id o checkout_url).')
            )

        _logger.info(
            'Nave: payment request creado para tx %s. ID: %s',
            tx.reference, response['id'],
        )
        return response

    def _nave_get_payment_request(self, payment_request_id):
        """Consulta el estado de una intención de pago en Nave."""
        url = f'{_API_URL[self._nave_env()]}/payment_requests/{payment_request_id}'
        return self._nave_request('GET', url)

    def _nave_get_payment(self, payment_id):
        """Obtiene los detalles de un pago por su ID."""
        url = f'{_PAYMENTS_URL[self._nave_env()]}/{payment_id}'
        return self._nave_request('GET', url)

    def _nave_get_payment_internal(self, payment_id):
        """Obtiene los detalles internos de un pago (incluye payment_code)."""
        url = f'{_PAYMENTS_URL[self._nave_env()]}/{payment_id}/internal'
        return self._nave_request('GET', url)

    # ── Body builders ────────────────────────────────────────────────────────

    def _nave_build_products(self, order, currency):
        if not order:
            return []
        products = []
        for line in order.order_line.filtered(lambda l: not l.display_type):
            products.append({
                'name':        line.product_id.name or line.name,
                'description': (
                    line.product_id.description_sale
                    or line.product_id.name
                    or line.name
                ),
                'quantity':   int(line.product_uom_qty),
                'unit_price': {
                    'currency': currency,
                    'value':    f'{line.price_unit:.2f}',
                },
            })
        # Envío
        if order.amount_delivery and order.amount_delivery > 0:
            products.append({
                'name':        'Envío',
                'description': 'Costo de envío',
                'quantity':    1,
                'unit_price':  {
                    'currency': currency,
                    'value':    f'{order.amount_delivery:.2f}',
                },
            })
        return products

    def _nave_build_buyer(self, tx):
        partner = tx.partner_id
        phone   = partner.phone or partner.mobile or ''
        if phone and not phone.startswith('+'):
            phone = '+54' + phone.lstrip('0')
        return {
            'user_id':    str(partner.id),
            'session_id': tx.reference,
            'name':       partner.name or '',
            'user_email': partner.email or '',
            'doc_type':   'DNI',
            'doc_number': '',
            'phone':      phone,
            'billing_address': {
                'street_1': partner.street or '',
                'street_2': partner.street2 or 'N/A',
                'city':     partner.city or '',
                'region':   partner.state_id.name if partner.state_id else '',
                'country':  partner.country_id.code if partner.country_id else '',
                'zipcode':  partner.zip or '',
            },
        }
