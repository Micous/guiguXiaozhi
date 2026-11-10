package com.atguigu.java.ai.langchain4j.service;

/**
 * 号源（排班）查询服务 —— 对接医院 HIS / 统一挂号平台的扩展点。
 * <p>
 * <b>这一层是 Mock</b>：真实环境要访问医院 HIS / 统一挂号平台，本地调不通，
 * 因此项目提供 {@link com.atguigu.java.ai.langchain4j.service.mock.MockSlotService}，
 * 完全在本地内存中模拟排班规则，不会访问任何外部系统。
 * <p>
 * 真实改造时：新增一个 {@code HisSlotService implements SlotService}（调用医院接口），
 * 并在 {@code MedicalServiceConfig} 中用 {@code @Primary} 或调整 Bean 装配替换掉 Mock 即可，
 * 工具层代码无需改动。
 */
public interface SlotService {

    /**
     * 查询某科室在指定日期、时段的号源。
     *
     * @param department 科室名称
     * @param date       日期，格式 2026-11-20
     * @param time       时段，上午 / 下午
     * @param doctorName 医生姓名，可为空（表示该科室任意医生）
     * @return 号源查询结果
     */
    SlotAvailability check(String department, String date, String time, String doctorName);
}
