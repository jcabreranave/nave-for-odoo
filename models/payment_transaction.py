import hmac
import logging
import secrets

from odoo import _, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

# ── Mapeos de estado ──────────────────────────────────────────────────────────

# Estado del PAYMENT de Nave → método de transición en Odoo
# Equivalente a OrderStateResolver.PAYMENT_STATUS_MAP del plugin de WooCommerce
_PAYMENT_STATUS_MAP = {
    'APPROVED':           'done',
    'REJECTED':           'pending',
    'CANCELLED':          'cancel',
    'REFUNDED':           'cancel',
    'PARTIALLY_REFUNDED': 'done',    # devolución parcial — orden sigue activa
    'PURCHASE_REVERSED':  'cancel',
    'CHARGEBACK_REVIEW':  'pending', # disputa activa — suspender
    'CHARGED_BACK':       'cancel',
}

# Estados finales del PAYMENT — no pueden cambiar más
_PAYMENT_FINAL_STATUSES = {
    'REJECTED', 'CANCELLED', 'REFUNDED', 'PURCHASE_REVERSED', 'CHARGED_BACK',
}

# Estado de la INTENCIÓN de Nave → método de transición en Odoo (fallback)
# Se usa solo cuando no hay payment disponible (intención expiró antes de que el cliente pagara)
_INTENT_STATUS_MAP = {
    'PENDING':           'pending',
    'PROCESSED':         'pending',
    'SUCCESS_PROCESSED': 'done',
    'FAILURE_PROCESSED': 'pending',
    'EXPIRED':           'cancel',
    'DISABLED':          'cancel',
    'BLOCKED':           'cancel',
}

# Estados finales de la INTENCIÓN
_INTENT_FINAL_STATUSES = {
    'SUCCESS_PROCESSED', 'EXPIRED', 'DISABLED', 'BLOCKED',
}


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    # ── Metadatos Nave ────────────────────────────────────────────────────────

    nave_payment_request_id = fields.Char(
        string='Nave Payment Request ID',
        readonly=True,
        copy=False,
    )
    nave_checkout_url = fields.Char(
        string='Nave Checkout URL',
        readonly=True,
        copy=False,
    )
    nave_status = fields.Char(
        string='Nave Estado Intención',
        readonly=True,
        copy=False,
    )
    nave_payment_id = fields.Char(
        string='Nave Payment ID',
        readonly=True,
        copy=False,
    )
    nave_payment_status = fields.Char(
        string='Nave Estado Pago',
        readonly=True,
        copy=False,
    )
    nave_payment_code = fields.Char(
        string='Nave Número de Operación',
        readonly=True,
        copy=False,
        help='Número de operación para identificar el pago ante cualquier reclamo.',
    )
    nave_callback_token = fields.Char(
        string='Nave Callback Token',
        readonly=True,
        copy=False,
        groups='base.group_system',
    )
    nave_webhook_secret = fields.Char(
        string='Nave Webhook Secret',
        readonly=True,
        copy=False,
        groups='base.group_system',
    )
    nave_webhook_received_at = fields.Datetime(
        string='Nave Webhook Recibido',
        readonly=True,
        copy=False,
    )

    # ── Token helpers ─────────────────────────────────────────────────────────

    def _nave_generate_callback_token(self):
        """Genera y persiste un token de callback de un solo uso (256 bits)."""
        token = secrets.token_hex(32)
        self.nave_callback_token = token
        return token

    def _nave_generate_webhook_secret(self):
        """Genera y persiste un secret para el webhook (256 bits)."""
        secret = secrets.token_hex(32)
        self.nave_webhook_secret = secret
        return secret

    # ── Punto de entrada del checkout ────────────────────────────────────────

    def _get_specific_rendering_values(self, processing_values):
        """
        Crea la intención de pago en Nave y retorna la URL de redirección.
        Equivalente a NaveGateway.process_payment() del plugin de WooCommerce.
        """
        res = super()._get_specific_rendering_values(processing_values)
        if self.provider_code != 'nave':
            return res

        try:
            payment_request = self.provider_id._nave_create_payment_request(self)

            self.write({
                'nave_payment_request_id': payment_request.get('id', ''),
                'nave_checkout_url':       payment_request.get('checkout_url', ''),
                'nave_status':             'created',
            })

            return {'checkout_url': self.nave_checkout_url}

        except ValidationError:
            raise
        except Exception as e:
            _logger.error(
                'Nave: error inesperado en _get_specific_rendering_values para tx %s: %s',
                self.reference, str(e),
            )
            raise ValidationError(
                _('Error al iniciar el pago con Nave. Por favor, intentá de nuevo.')
            )

    # ── Procesamiento de retorno ──────────────────────────────────────────────

    def _nave_process_return(self):
        """
        Consulta el estado real en Nave y actualiza la transacción.
        Llamado desde el controlador /nave/return después del redirect del cliente.
        Equivalente a ReturnHandler.process_order() del plugin de WooCommerce.
        """
        self.ensure_one()
        if not self.nave_payment_request_id:
            _logger.error('Nave: tx %s sin nave_payment_request_id', self.reference)
            return False

        # Idempotencia: no reprocesar si ya está en estado final
        if self.nave_status and self.nave_status in _INTENT_FINAL_STATUSES:
            if self.state in ('done', 'cancel'):
                _logger.info(
                    'Nave: tx %s ya en estado final (%s). Sin reprocesar.',
                    self.reference, self.nave_status,
                )
                return True

        return self._nave_fetch_and_update(update_state=True)

    def _nave_refresh(self):
        """
        Refresco manual desde el backend (equivalente a ReturnHandler.refresh_order_data()).
        Siempre consulta ambas APIs sin idempotencia.
        """
        self.ensure_one()
        if not self.nave_payment_request_id:
            return False
        _logger.info('Nave: refresco manual de tx %s', self.reference)
        return self._nave_fetch_and_update(update_state=False)

    def _nave_fetch_and_update(self, update_state=True):
        """
        Lógica compartida: consulta payment_request + payment y actualiza la transacción.
        Equivalente a ReturnHandler.fetch_and_update() del plugin de WooCommerce.

        El estado del PAYMENT es la fuente de verdad.
        El estado de la intención se usa como fallback si no hay payments.
        """
        try:
            provider = self.provider_id
            response = provider._nave_get_payment_request(self.nave_payment_request_id)

            nave_status = response.get('status', {}).get('name', 'PENDING')
            self.nave_status = nave_status

            _logger.info('Nave: estado intención para tx %s: %s', self.reference, nave_status)

            # Buscar el último payment de la intención
            payments = [
                p for p in response.get('payment_attempts', {}).get('payments', [])
                if p.get('payment_id')
            ]

            has_payment    = False
            payment_status = ''
            payment_code   = ''
            payment_id     = ''

            if payments:
                last_payment = payments[-1]
                payment_id   = last_payment['payment_id']

                try:
                    payment_data   = provider._nave_get_payment(payment_id)
                    payment_status = payment_data.get('status', {}).get('name', '')
                    payment_code   = payment_data.get('payment_code', '')

                    vals = {'nave_payment_id': payment_id}
                    if payment_status:
                        vals['nave_payment_status'] = payment_status
                        has_payment = True
                    if payment_code:
                        vals['nave_payment_code'] = payment_code

                    self.write(vals)

                    _logger.info(
                        'Nave: estado payment para tx %s (último de %d): %s',
                        self.reference, len(payments), payment_status,
                    )
                except Exception as pe:
                    _logger.warning(
                        'Nave: no se pudo obtener datos del payment para tx %s: %s',
                        self.reference, str(pe),
                    )

            if update_state:
                if has_payment:
                    self._nave_resolve_by_payment_status(payment_status)
                else:
                    # Fallback: usar estado de la intención
                    self._nave_resolve_by_intent_status(nave_status)

            return True

        except Exception as e:
            _logger.error(
                'Nave: error al consultar estado de tx %s: %s',
                self.reference, str(e),
            )
            self._set_error(
                _('[Nave] Error al consultar el estado del pago: %s') % str(e)
            )
            return False

    # ── Máquina de estados ────────────────────────────────────────────────────

    def _nave_resolve_by_payment_status(self, payment_status):
        """
        Aplica la transición de estado de Odoo basándose en el estado del PAYMENT.
        Equivalente a OrderStateResolver.resolve_by_payment_status() del plugin de WooCommerce.
        """
        if not payment_status:
            return

        odoo_state = _PAYMENT_STATUS_MAP.get(payment_status)

        if odoo_state == 'done':
            if not self.is_post_processed:
                _logger.info('Nave: tx %s → done (payment: %s)', self.reference, payment_status)
                self._set_done()
        elif odoo_state == 'cancel':
            _logger.info('Nave: tx %s → cancel (payment: %s)', self.reference, payment_status)
            self._set_canceled(state_message=_('[Nave] Pago %s') % payment_status)
        elif odoo_state == 'pending':
            if self.state not in ('done', 'cancel'):
                _logger.info('Nave: tx %s → pending (payment: %s)', self.reference, payment_status)
                self._set_pending()
        else:
            _logger.info(
                'Nave: tx %s — estado transitorio %s, sin transición.',
                self.reference, payment_status,
            )

    def _nave_resolve_by_intent_status(self, nave_status):
        """
        Aplica la transición basándose en el estado de la INTENCIÓN.
        Fallback cuando no hay payment disponible.
        Equivalente a OrderStateResolver.resolve() del plugin de WooCommerce.
        """
        odoo_state = _INTENT_STATUS_MAP.get(nave_status)

        if odoo_state == 'done':
            if not self.is_post_processed:
                _logger.info(
                    'Nave: tx %s → done (intención: %s)', self.reference, nave_status,
                )
                self._set_done()
        elif odoo_state == 'cancel':
            _logger.info('Nave: tx %s → cancel (intención: %s)', self.reference, nave_status)
            self._set_canceled(state_message=_('[Nave] Intención %s') % nave_status)
        elif odoo_state == 'pending':
            if self.state not in ('done', 'cancel'):
                _logger.info(
                    'Nave: tx %s → pending (intención: %s)', self.reference, nave_status,
                )
                self._set_pending()

    # ── Procesamiento del webhook ─────────────────────────────────────────────

    def _nave_process_webhook(self, payment_id, payment_status, payment_code=''):
        """
        Procesa una notificación server-to-server desde Nave.
        Equivalente a WebhookHandler.process_webhook() del plugin de WooCommerce.
        """
        self.ensure_one()

        from odoo.fields import Datetime
        vals = {'nave_webhook_received_at': Datetime.now()}

        if payment_id:
            vals['nave_payment_id'] = payment_id
        if payment_status:
            vals['nave_payment_status'] = payment_status
        if payment_code:
            vals['nave_payment_code'] = payment_code

        self.write(vals)

        _logger.info(
            'Nave: webhook procesado para tx %s — payment_status: %s',
            self.reference, payment_status,
        )

        self._nave_resolve_by_payment_status(payment_status)

    # ── Cron de polling ───────────────────────────────────────────────────────

    @models.api.model
    def _nave_cron_poll_pending(self):
        """
        Safety net: consulta el estado de transacciones Nave que quedaron en 'draft' o 'pending'
        después de un tiempo prudencial (webhook no llegó o cliente cerró el navegador).
        Equivalente a CronHandler del plugin de WooCommerce.
        """
        from datetime import timedelta
        from odoo.fields import Datetime

        min_age = timedelta(minutes=30)
        max_age = timedelta(minutes=25)   # ~duration_time por defecto (15 min) + buffer

        now = Datetime.now()
        date_from = now - timedelta(hours=4)   # no procesar órdenes de más de 4 horas
        date_to   = now - min_age

        pending_txs = self.search([
            ('provider_code',  '=',      'nave'),
            ('state',          'in',     ['draft', 'pending']),
            ('create_date',    '>=',     date_from),
            ('create_date',    '<=',     date_to),
            ('nave_payment_request_id', '!=', False),
        ])

        if not pending_txs:
            return

        _logger.info('Nave cron: %d transacciones pendientes encontradas.', len(pending_txs))

        for tx in pending_txs:
            # Saltar si el webhook llegó hace menos de 5 minutos
            if tx.nave_webhook_received_at:
                delta = now - tx.nave_webhook_received_at
                if delta.total_seconds() < 300:
                    _logger.info(
                        'Nave cron: tx %s resuelta por webhook recientemente. Saltando.',
                        tx.reference,
                    )
                    continue

            _logger.info('Nave cron: consultando estado de tx %s', tx.reference)
            try:
                tx._nave_process_return()
            except Exception as e:
                _logger.error(
                    'Nave cron: error procesando tx %s: %s', tx.reference, str(e),
                )
