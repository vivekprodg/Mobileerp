import uuid
from decimal import Decimal
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, permissions
from apps.pos.models import POSHoldCart
from apps.pos.api.serializers import POSHoldCartSerializer


class HoldCartListCreateAPIView(APIView):
    """
    API endpoint to list and suspend (park) carts with support for dual-IMEI tracking metadata.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        branch = getattr(request, 'active_branch', None)
        qs = POSHoldCart.objects.all()
        if branch:
            qs = qs.filter(branch=branch)
        serializer = POSHoldCartSerializer(qs, many=True)
        return Response({'held_carts': serializer.data})

    def post(self, request):
        branch = getattr(request, 'active_branch', None)
        if not branch:
            return Response({'error': 'Active branch required.'}, status=status.HTTP_400_BAD_REQUEST)

        cart_data = request.data.get('cart', [])
        subtotal = Decimal(str(request.data.get('subtotal', 0)))
        cust_name = request.data.get('customer_name', '').strip()
        cust_phone = request.data.get('customer_phone', '').strip()
        notes = request.data.get('notes', '')

        # Validate that any IMEI-tracked items in cart have their primary IMEI set
        for item in cart_data:
            if item.get('requires_imei') and not item.get('imei_number'):
                return Response({
                    'error': f"Item '{item.get('name')}' is missing primary IMEI 1."
                }, status=status.HTTP_400_BAD_REQUEST)

        hold_obj = POSHoldCart.objects.create(
            hold_reference=f"HOLD-{uuid.uuid4().hex[:6].upper()}",
            branch=branch,
            cashier=request.user,
            customer_name=cust_name or 'Walk-in',
            customer_phone=cust_phone,
            cart_payload={'items': cart_data},
            subtotal=subtotal,
            notes=notes
        )

        return Response({
            'status': 'success',
            'hold_reference': hold_obj.hold_reference,
            'message': 'Cart successfully parked.'
        }, status=status.HTTP_201_CREATED)


class HoldCartRecallDeleteAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, reference):
        cart = POSHoldCart.objects.filter(hold_reference=reference).first()
        if not cart:
            return Response({'error': 'Held cart not found.'}, status=status.HTTP_404_NOT_FOUND)
        serializer = POSHoldCartSerializer(cart)
        return Response(serializer.data)

    def delete(self, request, reference):
        cart = POSHoldCart.objects.filter(hold_reference=reference).first()
        if cart:
            cart.delete()
            return Response({'status': 'deleted'})
        return Response({'error': 'Held cart not found.'}, status=status.HTTP_404_NOT_FOUND)