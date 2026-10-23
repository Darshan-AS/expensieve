"""Unit tests for Gmail/PDF receipt extractors and merchant web order matchers."""

import unittest
from datetime import UTC, datetime

from expensieve.adapters.collectors.chrome_cdp import (
    CachedMerchantOrderCollector,
    classify_zomato_category,
    format_zomato_dishes,
    parse_zepto_order_detail_text,
)
from expensieve.adapters.collectors.gmail_imap import (
    AmazonEmailExtractor,
    EmailMessagePayload,
    HdfcCreditCardExtractor,
    InvestmentEmailExtractor,
    NammaYatriPdfExtractor,
    RapidoReceiptExtractor,
    SwiggyReceiptExtractor,
    TravelBookingExtractor,
)
from expensieve.adapters.collectors.pdf_text import rc4_crypt
from expensieve.adapters.statements.hdfc_cc_pdf import (
    HdfcCreditCardPdfStatementSource,
    parse_hdfc_cc_pdf_bytes,
)
from expensieve.domain.models import (
    CanonicalTransaction,
    DebtLinkGroup,
    PaymentMethod,
    StatementRow,
    TransactionKind,
)
from expensieve.domain.rules import DeclarativeRuleCollector


def _make_row(
    row_index: int,
    date_iso: str,
    narration: str,
    debit_minor: int = 0,
    credit_minor: int = 0,
    ref: str = "0000600000000001",
) -> StatementRow:
    return StatementRow(
        row_index=row_index,
        date_iso=date_iso,
        value_date_iso=date_iso,
        narration=narration,
        reference_no=ref,
        debit_minor=debit_minor,
        credit_minor=credit_minor,
        closing_balance_minor=1000000,
    )


class TestGmailReceiptExtractors(unittest.TestCase):
    def test_swiggy_extractor_with_items_and_restaurant(self) -> None:
        extractor = SwiggyReceiptExtractor()
        row = _make_row(
            row_index=10,
            date_iso="2026-09-27",
            narration="UPI-SWIGGY-SWIGGY8.RZP@AXIS-627012345678-PAID VIA CRED",
            debit_minor=18800,
        )
        msg = EmailMessagePayload(
            uid="1",
            sent_utc=datetime(2026, 9, 27, 9, 59, tzinfo=UTC),
            date_iso="2026-09-27",
            sender="Swiggy <noreply@swiggy.in>",
            subject="Your Swiggy order was delivered before time",
            text_body=(
                "Delivery in 40 mins! ORDER JOURNEY Auntie Fung's NO 53 SUBBAYANAPALYA 60 FEET ROAD "
                "Sep 27, 2:48 PM Order ID: 249470335153607 BILL DETAILS Teriyaki Ramen Veg x1 ₹289 "
                "Restaurant Packaging ₹10 Paid Via Cred ₹188 Disclaimer"
            ),
            attachments=(),
        )
        evs = extractor.extract([msg], [row])
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].row_index, 10)
        self.assertEqual(evs[0].category, "Meals & Fast Food")
        self.assertEqual(evs[0].payee, "Swiggy")
        self.assertEqual(evs[0].note, "1x Teriyaki Ramen Veg (Auntie Fung's)")
        self.assertFalse(evs[0].needs_review)

    def test_namma_yatri_line_parser_and_gazetteer(self) -> None:
        sample_lines = [
            "Hey Darshan A S, here's your invoice",
            "2 rides taken on Oct 11 - Oct 19, 2026",
            "Total Fare",
            "₹435.00",
            "19 Oct, 2026",
            "Ride Fare: ₹94",
            "9:18 PM",
            "2345, Kodandarama Reddy Layout, Ramamurthy Nagar, Bengaluru, Karnataka",
            "9:27 PM",
            "7th Cross Rd, Horamavu, Raghavendra Nagar, Bengaluru, Karnataka",
            "DRIVER NAME",
            "O*K*S*AN",
            "11 Oct, 2026",
            "Ride Fare: ₹341",
            "11:39 AM",
            "7th Cross Rd, Horamavu, Raghavendra Nagar, Bengaluru, Karnataka",
            "12:31 PM",
            "Jawaharlal Nehru Planetarium, Sankey Road, High Grounds, Bengaluru, Karnataka",
            "DRIVER NAME",
            "MOHAMMED KHADER",
        ]
        rides = NammaYatriPdfExtractor._parse_rides_from_lines(sample_lines)
        self.assertEqual(len(rides), 2)
        self.assertEqual(rides[0][0], "2026-10-19")
        self.assertEqual(rides[0][1], 9400)
        self.assertEqual(rides[1][0], "2026-10-11")
        self.assertEqual(rides[1][1], 34100)

    def test_rapido_receipt_extractor_html_and_lines(self) -> None:
        extractor = RapidoReceiptExtractor()
        row = _make_row(
            row_index=25,
            date_iso="2026-06-15",
            narration="UPI-SANTHOSH KUMAR N-Q123@YBL-616612345678-RAPIDO",
            debit_minor=15500,
        )
        msg = EmailMessagePayload(
            uid="2",
            sent_utc=datetime(2026, 6, 22, 6, 37, tzinfo=UTC),
            date_iso="2026-06-22",
            sender="Rapido <shoutout@rapido.bike>",
            subject="Your trip with Rapido",
            text_body=(
                "Booking History Customer Name Darshan A S Ride ID RD17814949592469166 "
                "Driver name Santhosh Kumar N Vehicle Number KA03AP0395 Mode of Vehicle Auto "
                "Time of Ride Jun 15th 2026, 9:20 AM Selected Price ₹ 155 "
                "Geetha Murali Illam, 25, 5th Cross Rd, Raghavendra Nagar, Horamavu, Bengaluru, Karnataka 560016, India "
                "22/2, Krishna Reddy Industrial Estate, Dooravani Nagar, Bengaluru, Karnataka 560016, India "
                "This document is issued on request by the passenger."
            ),
            attachments=(),
        )
        evs = extractor.extract([msg], [row])
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].row_index, 25)
        self.assertEqual(evs[0].category, "Auto")
        self.assertEqual(evs[0].payee, "Rapido")
        self.assertEqual(evs[0].note, "Bangalore Home to Tin Factory")

    def test_amazon_email_extractor_combined_and_pharmacy(self) -> None:
        extractor = AmazonEmailExtractor()
        row_meds = _make_row(
            row_index=629,
            date_iso="2026-09-06",
            narration="UPI-AMAZON INDIA-AMAZON@APL-624912345678-AMAZON",
            debit_minor=201252,
        )
        row_multi = _make_row(
            row_index=295,
            date_iso="2026-05-16",
            narration="UPI-AMAZON INDIA-AMAZON@APL-613612345678-AMAZON",
            debit_minor=90700,
        )
        msg_meds = EmailMessagePayload(
            uid="3",
            sent_utc=datetime(2026, 9, 6, 16, 19, tzinfo=UTC),
            date_iso="2026-09-06",
            sender="auto-confirm@amazon.in",
            subject="Ordered: Eptus 50",
            text_body=(
                "Order #\n171-3271967-0616305\n"
                "* Eptus 50 - Strip of 10 Tablets\n  Quantity: 1\n  586.95 INR\n"
                "* Dolo-650 - Strip of 15 Tablets\n  Quantity: 1\n  31.15 INR\n"
                "Total\n2012.52 INR\n"
            ),
            attachments=(),
        )
        msg_multi = EmailMessagePayload(
            uid="4",
            sent_utc=datetime(2026, 5, 16, 5, 40, tzinfo=UTC),
            date_iso="2026-05-16",
            sender="auto-confirm@amazon.in",
            subject="Ordered: Mirabelle Fairness Face...",
            text_body=(
                "Order #\n171-7664625-0901150\n"
                "* Mirabelle Fairness Face Mask Sheets | Korean Sheet Mask\n  Quantity: 1\n  327 INR\n"
                "Total\n599.3 INR\n"
                "Order #\n171-6011219-2521949\n"
                "* Lakme Forever Matte Compact Powder\n  Quantity: 1\n  143 INR\n"
                "Total\n307.7 INR\n"
            ),
            attachments=(),
        )
        evs = extractor.extract([msg_meds, msg_multi], [row_meds, row_multi])
        self.assertEqual(len(evs), 2)
        by_row = {e.row_index: e for e in evs}
        self.assertEqual(by_row[629].category, "Drug-store, chemist")
        self.assertIn("1x Eptus 50", by_row[629].note or "")
        self.assertEqual(by_row[295].category, "Wellness, beauty")
        self.assertIn("1x Mirabelle Fairness Face Mask Sheets", by_row[295].note or "")
        self.assertIn("1x Lakme Forever Matte Compact Powder", by_row[295].note or "")

    def test_travel_and_investment_extractors(self) -> None:
        travel_ext = TravelBookingExtractor()
        row_train = _make_row(
            row_index=272,
            date_iso="2026-04-28",
            narration="UPI-CONFIRMTKT-CONFIRMTKT@HDFCBANK-611812345678-TRAIN",
            debit_minor=15800,
        )
        msg_train = EmailMessagePayload(
            uid="5",
            sent_utc=datetime(2026, 4, 28, 12, 23, tzinfo=UTC),
            date_iso="2026-04-28",
            sender="ConfirmTkt Support <support@confirmtkt.com>",
            subject="Train Ticket -TK952601855A48",
            text_body=(
                "Your IRCTC e-ticket between (SBC) and (ASK) on 30 Apr 2026 has been booked.\n"
                "PNR No.\nConfirmtkt Booking ID\n4857255872\nTK952601855A48\n"
                "Train Name & No.\nClass/Quota\nJANSHATABDI EXP/12089\n2S/GN\n"
                "Total Fare\n₹ 158.0\n"
            ),
            attachments=(),
        )
        t_evs = travel_ext.extract([msg_train], [row_train])
        self.assertEqual(len(t_evs), 1)
        self.assertEqual(t_evs[0].category, "Train")
        self.assertEqual(t_evs[0].payee, "ConfirmTkt")
        self.assertIn("Bangalore (SBC) to Arsikere (ASK)", t_evs[0].note or "")
        self.assertIn("PNR 4857255872", t_evs[0].note or "")

        inv_ext = InvestmentEmailExtractor()
        row_ipo = _make_row(
            row_index=461,
            date_iso="2026-07-18",
            narration="UPI-SBI FUNDS MANAGEMENT-IPO@KFIN-619912345678-IPO",
            debit_minor=1492400,
        )
        msg_ipo = EmailMessagePayload(
            uid="6",
            sent_utc=datetime(2026, 7, 18, 11, 1, tzinfo=UTC),
            date_iso="2026-07-18",
            sender="KFIN TECHNOLOGIES LIMITED <sbifml.ipo@kfintech.com>",
            subject="SBI FUNDS MANAGMENT LIMITED-IPO",
            text_body=(
                "Bid cum Application Form No.\n68857622\n"
                "Issue Price per Security (Rs.)\n574\n"
                "Shares Allotted\n26 Shares being credited\n"
                "Amount adjusted towards allotment (Rs.)\n14924.00\n"
            ),
            attachments=(),
        )
        i_evs = inv_ext.extract([msg_ipo], [row_ipo])
        self.assertEqual(len(i_evs), 1)
        self.assertEqual(i_evs[0].kind, TransactionKind.TRANSFER)
        self.assertEqual(i_evs[0].transfer_account, "Investments")
        self.assertEqual(i_evs[0].payee, "IndMoney")
        self.assertIn("26 shares @ ₹574, Appl 68857622", i_evs[0].note or "")


class TestMerchantOrderCollectors(unittest.TestCase):
    def test_zomato_dish_formatting_and_matching(self) -> None:
        self.assertEqual(
            format_zomato_dishes("1 x Paper Masala Dosa, 2 x Filter Coffee"),
            "1x Paper Masala Dosa\n2x Filter Coffee",
        )
        self.assertEqual(
            classify_zomato_category("1x Belgian Chocolate Ice Cream", "Polar Bear"),
            "Desserts & Sweets",
        )
        self.assertEqual(
            classify_zomato_category("1x Masala Dosa", "Udupi Mane Thindi"),
            "Meals & Fast Food",
        )

        collector = CachedMerchantOrderCollector()
        rows = [
            _make_row(1, "2026-10-21", "UPI-ZOMATO-629412345678-ZOMATO", debit_minor=9209),
            _make_row(2, "2026-02-02", "UPI-ZOMATO-603312345678-MANDATE", debit_minor=100),
        ]
        orders = [
            {
                "orderId": 8745016695,
                "totalCost": "₹92.09",
                "orderDate": "October 21, 2026 at 09:14 AM",
                "dishString": "1 x Paper Masala Dosa",
                "status": 6,
                "paymentStatus": 1,
                "resName": "Udupi Mane Thindi",
            }
        ]
        evs = collector.match_zomato_orders(rows, orders)
        self.assertEqual(len(evs), 2)
        self.assertEqual(evs[0].note, "1x Paper Masala Dosa (Udupi Mane Thindi)")
        self.assertEqual(evs[1].note, "Zomato UPI mandate verification")

    def test_zepto_and_blinkit_matching(self) -> None:
        raw_zepto_page = (
            "Order #JMSJKBLEK85668\n2 items\nDelivered\n2 items in order\n\n"
            "Niftas 100mg Tablet SR\n\n14 Tablets\n\n1 unit\n\n₹110\n\n₹122.01\n\n"
            "Otrivin Oxy Fast Relief Adult Nasal Spray\n\n10 mL\n\n1 unit\n\n₹120\n\n₹132.86\n\n"
            "Rate Order\nOrder Again"
        )
        note, cat = parse_zepto_order_detail_text(raw_zepto_page)
        self.assertEqual(cat, "Drug-store, chemist")
        self.assertEqual(
            note,
            "1x Niftas 100mg Tablet SR\n1x Otrivin Oxy Fast Relief Adult Nasal Spray",
        )

        collector = CachedMerchantOrderCollector()
        b_row = _make_row(50, "2026-01-24", "UPI-BLINKIT-602412345678-BLINKIT", debit_minor=28000)
        b_orders = [
            {
                "dateStr": "24 Jan, 10:15 am",
                "amount": "₹180",
                "items": [{"name": "Nandini Curd", "quantity": 2}],
            },
            {
                "dateStr": "24 Jan, 10:15 am",
                "amount": "₹100",
                "items": [{"name": "Whole Wheat Bread", "quantity": 1}],
            },
        ]
        b_evs = collector.match_blinkit_orders([b_row], b_orders, default_year=2026)
        self.assertEqual(len(b_evs), 1)
        self.assertEqual(b_evs[0].note, "2x Nandini Curd\n1x Whole Wheat Bread")


class TestHdfcCreditCardPdfAndAlerts(unittest.TestCase):
    def test_rc4_roundtrip(self) -> None:
        key = b"DARS3835"
        plain = b"HDFC Millennia Credit Card Statement"
        cipher = rc4_crypt(key, plain)
        self.assertNotEqual(cipher, plain)
        self.assertEqual(rc4_crypt(key, cipher), plain)

    def test_hdfc_cc_pdf_parser_and_statement_source(self) -> None:
        raw_stream = (
            b"BT 1 0 0 1 20 -80.1 Tm (Statement Date:15/06/2025) Tj ET\n"
            b"BT 1 0 0 1 271.5 -211.6 Tm (Previous Balance) Tj ET\n"
            b"BT 1 0 0 1 533.1 -211.6 Tm (Total Dues) Tj ET\n"
            b"BT 1 0 0 1 271.5 -236.6 Tm (0.49) Tj ET\n"
            b"BT 1 0 0 1 341.1 -236.6 Tm (0.00) Tj ET\n"
            b"BT 1 0 0 1 397.1 -236.6 Tm (928.00) Tj ET\n"
            b"BT 1 0 0 1 477.1 -236.6 Tm (0.00) Tj ET\n"
            b"BT 1 0 0 1 533.1 -236.6 Tm (928.00) Tj ET\n"
            b"BT 1 0 0 1 248.5 -426.0 Tm (Domestic Transactions) Tj ET\n"
            b"BT 1 0 0 1 37.5 -478.0 Tm (17/05/2025) Tj ET\n"
            b"BT 1 0 0 1 112.0 -478.0 Tm (KAKE DI HATTI          BANGALORE) Tj ET\n"
            b"BT 1 0 0 1 523.0 -478.0 Tm (928.00) Tj ET\n"
            b"BT 1 0 0 1 37.5 -489.0 Tm (25/06/2025) Tj ET\n"
            b"BT 1 0 0 1 112.0 -489.0 Tm (TELE TRANSFER CREDIT \\(Ref# ST251770083000010071427\\)) Tj ET\n"
            b"BT 1 0 0 1 523.0 -489.0 Tm (928.00) Tj 1 0 0 1 548.0 -489.0 Tm (Cr) Tj ET\n"
        )
        synthetic_pdf = (
            b"%PDF-1.4\n1 0 obj\n<< /Length "
            + str(len(raw_stream)).encode()
            + b" >>\nstream\n"
            + raw_stream
            + b"\nendstream\nendobj\n%%EOF"
        )
        parsed = parse_hdfc_cc_pdf_bytes(synthetic_pdf, filename="15-06-2025.PDF")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.statement_date_iso, "2025-06-15")
        self.assertEqual(parsed.opening_balance_minor, -49)
        self.assertEqual(len(parsed.transactions), 2)

        source = HdfcCreditCardPdfStatementSource(pdf_payloads=[("15-06-2025.PDF", synthetic_pdf)])
        rows = source.parse()
        self.assertEqual(len(rows), 2)
        self.assertEqual(source.opening_balance_minor(), -49)
        self.assertEqual(rows[0].debit_minor, 92800)
        self.assertEqual(rows[0].closing_balance_minor, -92849)
        self.assertEqual(rows[1].credit_minor, 92800)
        self.assertEqual(rows[1].reference_no, "ST251770083000010071427")
        self.assertEqual(source.closing_balance_minor(), -49)

    def test_hdfc_cc_insta_alert_extractor(self) -> None:
        extractor = HdfcCreditCardExtractor()
        row = _make_row(
            row_index=1,
            date_iso="2025-05-17",
            narration="KAKE DI HATTI BANGALORE",
            debit_minor=92800,
        )
        msg = EmailMessagePayload(
            uid="78860",
            sent_utc=datetime(2025, 5, 17, 10, 15, 38, tzinfo=UTC),
            date_iso="2025-05-17",
            sender="HDFC Bank InstaAlerts <alerts@hdfcbank.net>",
            subject="Alert : Update on your HDFC Bank Credit Card",
            text_body=(
                "Dear Card Member, Thank you for using your HDFC Bank Credit Card ending 3835 "
                "for Rs 928.00 at KAKE DI HATTI on 17-05-2025 15:45:31. "
                "After the above transaction, the total outstanding is Rs 929.00"
            ),
            attachments=(),
        )
        evs = extractor.extract([msg], [row])
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].timestamp_iso, "2025-05-17T10:15:31Z")
        self.assertEqual(evs[0].payee, "Kake Di Hatti")
        self.assertEqual(evs[0].payment_method, PaymentMethod.CREDIT_CARD)

    def test_ref_linking_for_dcc_fcy_igst_and_membership_fees(self) -> None:
        raw_stream = (
            b"BT 1 0 0 1 20 -80.1 Tm (Statement Date:15/10/2026) Tj ET\n"
            b"BT 1 0 0 1 271.5 -211.6 Tm (Previous Balance) Tj ET\n"
            b"BT 1 0 0 1 533.1 -211.6 Tm (Total Dues) Tj ET\n"
            b"BT 1 0 0 1 271.5 -236.6 Tm (0.00) Tj ET\n"
            b"BT 1 0 0 1 341.1 -236.6 Tm (0.00) Tj ET\n"
            b"BT 1 0 0 1 397.1 -236.6 Tm (4,047.20) Tj ET\n"
            b"BT 1 0 0 1 477.1 -236.6 Tm (0.00) Tj ET\n"
            b"BT 1 0 0 1 533.1 -236.6 Tm (4,047.20) Tj ET\n"
            b"BT 1 0 0 1 248.5 -426.0 Tm (International Transactions) Tj ET\n"
            b"BT 1 0 0 1 37.5 -460.0 Tm (28/09/2026) Tj ET\n"
            b"BT 1 0 0 1 112.0 -460.0 Tm (KLOOK TRAVEL TECH \\(Ref# MT222630083000010256178\\)) Tj ET\n"
            b"BT 1 0 0 1 523.0 -460.0 Tm (4,000.00) Tj ET\n"
            b"BT 1 0 0 1 37.5 -472.0 Tm (28/09/2026) Tj ET\n"
            b"BT 1 0 0 1 112.0 -472.0 Tm (1% on all DCC Transaction \\(Ref# MT222630083000010256178\\)) Tj ET\n"
            b"BT 1 0 0 1 523.0 -472.0 Tm (40.00) Tj ET\n"
            b"BT 1 0 0 1 37.5 -484.0 Tm (28/09/2026) Tj ET\n"
            b"BT 1 0 0 1 112.0 -484.0 Tm (IGST-VPS2627 \\(Ref# MT222630083000010256178\\)) Tj ET\n"
            b"BT 1 0 0 1 523.0 -484.0 Tm (7.20) Tj ET\n"
        )
        synthetic_pdf = (
            b"%PDF-1.4\n1 0 obj\n<< /Length "
            + str(len(raw_stream)).encode()
            + b" >>\nstream\n"
            + raw_stream
            + b"\nendstream\nendobj\n%%EOF"
        )
        source = HdfcCreditCardPdfStatementSource(pdf_payloads=[("15-10-2026.PDF", synthetic_pdf)])
        rows = list(source.parse())
        self.assertEqual(len(rows), 3)
        self.assertIn("[Parent: KLOOK TRAVEL TECH]", rows[1].narration)
        self.assertIn("[Parent: 1% on all DCC Transaction - KLOOK TRAVEL TECH]", rows[2].narration)

        rule_evs = DeclarativeRuleCollector().collect(
            rows,
            rows[0].statement_date,
            rows[-1].statement_date,
        )
        by_idx = {e.row_index: e for e in rule_evs}
        self.assertEqual(by_idx[2].category, "Charges, fees")
        self.assertEqual(by_idx[2].payee, "HDFC")
        self.assertEqual(by_idx[2].note, "DCC markup fee (KLOOK TRAVEL TECH)")
        self.assertEqual(by_idx[3].category, "Charges, fees")
        self.assertEqual(by_idx[3].payee, "HDFC")
        self.assertEqual(
            by_idx[3].note,
            "IGST on 1% on all DCC Transaction - KLOOK TRAVEL TECH",
        )

        # Also verify Klook email extractor on ±1 day card swipe
        travel_ext = TravelBookingExtractor()
        klook_msg = EmailMessagePayload(
            uid="99",
            sent_utc=datetime(2026, 9, 27, 23, 50, tzinfo=UTC),
            date_iso="2026-09-27",
            sender="Klook <support@klook.com>",
            subject="Your booking for Gardens by the Bay Ticket",
            text_body="Booking confirmed",
            attachments=(),
        )
        k_evs = travel_ext.extract([klook_msg], rows)
        self.assertEqual(len(k_evs), 1)
        self.assertEqual(k_evs[0].row_index, 1)
        self.assertEqual(k_evs[0].payee, "Klook")
        self.assertEqual(k_evs[0].category, "Culture, sport events")
        self.assertEqual(k_evs[0].note, "Gardens by the Bay Ticket")

    def test_sourdough_sink_idempotent_dedup_and_open_debt_reuse(self) -> None:
        from expensieve.adapters.sinks.sourdough_sink import SourdoughLedgerSink

        # Check existing non-transfer 1-to-1 and 1-to-N split matching
        existing_raw = [
            {
                "_id": "Record_11111111-1111-4111-8111-111111111111",
                "_rev": "1-a",
                "reservedModelType": "Record",
                "accountId": "-Account_cc",
                "type": 1,
                "amount": 92800,
                "recordDate": "2025-05-17T00:00:00.000Z",
                "transfer": False,
                "note": "",
                "payee": "",
            },
            {
                "_id": "Record_22222222-2222-4222-8222-222222222222",
                "_rev": "1-b",
                "reservedModelType": "Record",
                "accountId": "-Account_cc",
                "type": 1,
                "amount": 60000,
                "recordDate": "2025-06-10T12:00:00.000Z",
                "transfer": False,
                "note": "Part 1",
                "payee": "Amazon",
            },
            {
                "_id": "Record_33333333-3333-4333-8333-333333333333",
                "_rev": "1-c",
                "reservedModelType": "Record",
                "accountId": "-Account_cc",
                "type": 1,
                "amount": 40000,
                "recordDate": "2025-06-10T12:00:00.000Z",
                "transfer": False,
                "note": "Part 2",
                "payee": "Amazon",
            },
        ]
        consumed: set[str] = set()
        m_one, split_one = SourdoughLedgerSink._find_existing_non_transfer_match(
            existing_raw_records=existing_raw,
            consumed_record_ids=consumed,
            account_id="-Account_cc",
            record_type=1,
            amount_minor=92800,
            timestamp_iso="2025-05-17T10:15:31Z",
        )
        self.assertIsNotNone(m_one)
        self.assertEqual(split_one, ())

        m_split, split_ids = SourdoughLedgerSink._find_existing_non_transfer_match(
            existing_raw_records=existing_raw,
            consumed_record_ids=consumed,
            account_id="-Account_cc",
            record_type=1,
            amount_minor=100000,
            timestamp_iso="2025-06-10T15:00:00Z",
        )
        self.assertIsNone(m_split)
        self.assertEqual(
            set(split_ids),
            {
                "Record_22222222-2222-4222-8222-222222222222",
                "Record_33333333-3333-4333-8333-333333333333",
            },
        )
        self.assertIsNotNone(CanonicalTransaction)
        self.assertIsNotNone(DebtLinkGroup)


if __name__ == "__main__":
    unittest.main()
