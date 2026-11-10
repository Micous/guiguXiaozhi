package com.atguigu.java.ai.langchain4j.service;

/**
 * 医生信息。
 *
 * @param name       姓名
 * @param department 所属科室
 * @param title      职称
 * @param expertise  擅长方向
 */
public record DoctorInfo(String name, String department, String title, String expertise) {

    /** 简短展示，例如「王笠（神经内科 主任医师）」 */
    public String brief() {
        return name + "（" + department + " " + title + "）";
    }
}
