import hmac
import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class NaveController(http.Controller):

    # ── Callback del cliente (return URL) ─────────────────────────────────────

    @http.route(
        '/nave/return',
        type='http',
        auth='public',
        csrf=False,
        save_session=False,
        methods=['GET'],
    )
    def nave_return(self, tx_ref=None, nave_token=None, **kwargs):
        """
        El cliente llega aquí después de completar (o abandonar) el pago en Nave.
        Equivalente a ReturnHandler.handle() del plugin de WooCommerce.

        Seguridad implementada:
        - nave_token: token criptográfico de un solo uso (256 bits) — anti-enumeración
        - tx_ref: referencia de la transacción Odoo — no el ID numérico (anti-IDOR)
        - hash_equals equivalent: hmac.compare_digest() — comparación timing-safe
        """
        if not tx_ref:
            _logger.warning('Nave return: sin tx_ref.')
            return request.redirect('/shop')

        tx = request.env['payment.transaction'].sudo().search(
            [('reference', '=', tx_ref), ('provider_code', '=', 'nave')],
            limit=1,
        )

        if not tx:
            _logger.warning('Nave return: tx %s no encontrada.', tx_ref)
            return request.redirect('/shop')

        # Validar token — timing-safe
        stored_token = tx.nave_callback_token or ''
        if not nave_token or not hmac.compare_digest(stored_token, nave_token):
            if not stored_token:
                # Token ya consumido — segundo intento legítimo (back button, refresh)
                _logger.info(
                    'Nave return: token ya consumido para tx %s. Redirigiendo al estado.',
                    tx_ref,
                )
            else:
                _logger.error(
                    'Nave return: token inválido para tx %s. Posible intento de enumeración.',
                    tx_ref,
                )
                return request.redirect('/shop')
            # Sin token válido, ir directo al estado (puede que ya esté procesada)
            return request.redirect('/payment/status')

        # Invalidar token (one-time use)
        tx.nave_callback_token = False

        # Procesar el estado en Nave y actualizar la transacción
        try:
            tx._nave_process_return()
        except Exception as e:
            _logger.error(
                'Nave return: error al procesar tx %s: %s', tx_ref, str(e),
            )

        return request.redirect('/payment/status')

    # ── Webhook server-to-server desde Nave ──────────────────────────────────

    @http.route(
        '/nave/webhook',
        type='http',
        auth='public',
        csrf=False,
        methods=['POST'],
    )
    def nave_webhook(self, tx_ref=None, secret=None, **kwargs):
        """
        Nave llama a este endpoint cuando el estado de un pago cambia.
        Equivalente a WebhookHandler.handle() del plugin de WooCommerce.

        Seguridad:
        - secret: token criptográfico de 256 bits por transacción — validado con hmac.compare_digest()
        - Siempre responde 200 para evitar reintentos de Nave sobre transacciones inexistentes.
        """
        def _json_response(data, status=200):
            return request.make_response(
                json.dumps(data),
                headers=[('Content-Type', 'application/json')],
                status=status,
            )

        if not tx_ref or not secret:
            _logger.warning('Nave webhook: request sin tx_ref o secret.')
            return _json_response({'error': 'missing_params'}, status=400)

        tx = request.env['payment.transaction'].sudo().search(
            [('reference', '=', tx_ref), ('provider_code', '=', 'nave')],
            limit=1,
        )

        if not tx:
            _logger.warning('Nave webhook: tx %s no encontrada.', tx_ref)
            return _json_response({'ok': True})  # 200 para evitar reintentos

        # Validar secret — timing-safe
        stored_secret = tx.nave_webhook_secret or ''
        if not hmac.compare_digest(stored_secret, secret):
            _logger.error('Nave webhook: secret inválido para tx %s.', tx_ref)
            return _json_response({'error': 'invalid_secret'}, status=401)

        # Parsear el body JSON
        try:
            body = json.loads(request.httprequest.get_data(as_text=True) or '{}')
        except json.JSONDecodeError:
            body = {}

        payment_id        = body.get('payment_id', '')
        payment_status    = ''
        payment_code      = ''

        if payment_id:
            try:
                payment_data   = tx.provider_id._nave_get_payment_internal(payment_id)
                payment_status = payment_data.get('status', {}).get('name', '')
                payment_code   = payment_data.get('payment_code', '')
            except Exception as e:
                _logger.error(
                    'Nave webhook: error al obtener payment %s para tx %s: %s',
                    payment_id, tx_ref, str(e),
                )

        _logger.info(
            'Nave webhook: tx %s — payment_id: %s, payment_status: %s',
            tx_ref, payment_id, payment_status,
        )

        try:
            tx._nave_process_webhook(
                payment_id=payment_id,
                payment_status=payment_status,
                payment_code=payment_code,
            )
        except Exception as e:
            _logger.error(
                'Nave webhook: error al procesar tx %s: %s', tx_ref, str(e),
            )

        return _json_response({'ok': True})
