select 
    DELIVERYDATE_DATE,
    TIMEFRAME_CODE_CODE,
    PRICE_TOKYO_VALUE,
    PRICE_KANSAI_VALUE,
    PRICE_CHUBU_VALUE,
from 
    app_rskm.app_cm_t_jepx_spot_price_data
where
    DELIVERYDATE_DATE >= '{__start_date__}' and
    DELIVERYDATE_DATE <= '{__end_date__}'
order by
    DELIVERYDATE_DATE, TIMEFRAME_CODE_CODE